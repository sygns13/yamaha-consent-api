"""Callback a KIZUNA (M5): el payload y un intento de envío. Los reintentos están en app/services/kizuna_notifications.py.

TODO(PENDIENTE-13): el contrato real (URL, autenticación, campos y literales de estado) lo define Yamaha/KIZUNA.
Pasar al endpoint real solo debe tocar las variables de entorno y build_kizuna_payload().
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.models import Consent

logger = logging.getLogger(__name__)

_ERROR_MAX_LENGTH = 1000
_BODY_MAX_LENGTH = 500


class KizunaConfigurationError(RuntimeError):
    """KIZUNA_CALLBACK_ENABLED=true con la configuración del callback incompleta: la aplicación no debe arrancar."""


def build_kizuna_payload(consent: Consent) -> dict[str, Any]:
    """Respuesta final de un ID de transacción, con el payload propuesto en la spec (M5).

    Única función que arma el payload. Cada registro (principal o copia vinculada, RN-04) se notifica con su propio
    transaction_id. En NO_ACEPTADO (RN-01) el marketing va en null.
    """
    deleted = consent.is_deleted == 1
    return {
        "transaction_id": consent.transaction_id,
        "document_type": consent.document_type.value,
        "document_number": consent.document_number,
        "dealer": consent.dealer,
        "status": consent.status.value,
        "consents": {
            "personal_data": consent.consent_personal_data,
            "marketing_yamaha": None if deleted else consent.consent_marketing_yamaha,
            "marketing_dealer": None if deleted else consent.consent_marketing_dealer,
        },
        "responded_at": _iso_utc(consent.responded_at),
    }


def _iso_utc(value: datetime | None) -> str | None:
    # Mismo formato que las consultas de KIZUNA (sección 2.4): ISO 8601 en UTC con sufijo Z.
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class KizunaSendResult:
    ok: bool
    # Error de red, timeout, 5xx o 429. Cualquier otra respuesta que no sea 2xx no se reintenta.
    retryable: bool
    http_status: int | None = None
    error: str | None = None


def is_retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


class KizunaClient:
    """Un POST a KIZUNA (o al mock). No lanza excepciones: todo resultado queda en KizunaSendResult."""

    def __init__(self, client: httpx.AsyncClient, *, timeout_seconds: float, headers: dict[str, str]) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._headers = headers

    @classmethod
    def build_http_client(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=settings.kizuna_callback_timeout_seconds, transport=transport, follow_redirects=False
        )

    @classmethod
    def from_settings(cls, client: httpx.AsyncClient, settings: Settings) -> "KizunaClient":
        # TODO(PENDIENTE-13): método de autenticación real. Mientras tanto, un header configurable.
        headers = {}
        if settings.kizuna_callback_auth_header.strip():
            headers[settings.kizuna_callback_auth_header.strip()] = (
                settings.kizuna_callback_auth_value.get_secret_value()
            )
        return cls(client, timeout_seconds=settings.kizuna_callback_timeout_seconds, headers=headers)

    async def send(self, url: str, payload: dict[str, Any]) -> KizunaSendResult:
        try:
            # El timeout de httpx es por operación (conexión, lectura...): este límite cubre el intento completo.
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._client.post(url, json=payload, headers=self._headers)
        except TimeoutError:
            return KizunaSendResult(
                ok=False, retryable=True, error=f"Timeout: sin respuesta en {self._timeout_seconds:g} s"
            )
        except httpx.TimeoutException as exc:
            return KizunaSendResult(
                ok=False, retryable=True, error=_truncate(f"Timeout: {type(exc).__name__}")
            )
        except httpx.HTTPError as exc:
            return KizunaSendResult(ok=False, retryable=True, error=_truncate(f"{type(exc).__name__}: {exc}"))

        if response.is_success:
            return KizunaSendResult(ok=True, retryable=False, http_status=response.status_code)
        # El cuerpo se guarda en last_error para diagnosticar (puede traer el documento: no va al log).
        body = response.text[:_BODY_MAX_LENGTH]
        return KizunaSendResult(
            ok=False,
            retryable=is_retryable_status(response.status_code),
            http_status=response.status_code,
            error=_truncate(
                f"HTTP {response.status_code}: {body}" if body else f"HTTP {response.status_code}"
            ),
        )


def _truncate(value: str) -> str:
    return value[:_ERROR_MAX_LENGTH]


def check_kizuna_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app.

    - KIZUNA_CALLBACK_ENABLED=false → log WARNING: las notificaciones quedan PENDIENTE y no sale ninguna petición.
    - KIZUNA_CALLBACK_ENABLED=true sin KIZUNA_CALLBACK_URL, o con KIZUNA_CALLBACK_AUTH_HEADER sin su valor →
      KizunaConfigurationError: la app no arranca (mismo criterio que la API Key y Twilio).
    """
    if not settings.kizuna_callback_enabled:
        logger.warning(
            "KIZUNA_CALLBACK_ENABLED=false: NO se envían respuestas a KIZUNA (las notificaciones quedan PENDIENTE)"
        )
        return

    problems = []
    if not settings.kizuna_callback_url.strip():
        problems.append("falta KIZUNA_CALLBACK_URL")
    if (
        settings.kizuna_callback_auth_header.strip()
        and not settings.kizuna_callback_auth_value.get_secret_value()
    ):
        problems.append("KIZUNA_CALLBACK_AUTH_HEADER está configurado pero falta KIZUNA_CALLBACK_AUTH_VALUE")
    if problems:
        message = f"KIZUNA_CALLBACK_ENABLED=true pero la configuración está incompleta: {'; '.join(problems)}"
        logger.critical(message)
        raise KizunaConfigurationError(message)

    # El reclamo de una notificación se renueva en cada intento. Si el plazo de reproceso es menor que la espera más
    # larga entre intentos (más el timeout), otra instancia que arranque podría reclamarla a mitad de los reintentos y
    # KIZUNA la recibiría dos veces.
    longest_gap = (
        max(settings.kizuna_callback_backoff_seconds, default=0) + settings.kizuna_callback_timeout_seconds
    )
    if settings.kizuna_callback_reprocess_after_minutes * 60 <= longest_gap:
        logger.warning(
            "KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES es menor que la espera más larga entre intentos: una notificación "
            "podría enviarse dos veces",
            extra={
                "reprocess_after_minutes": settings.kizuna_callback_reprocess_after_minutes,
                "longest_gap_seconds": longest_gap,
            },
        )
