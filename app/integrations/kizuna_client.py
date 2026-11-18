"""Callback a KIZUNA (M5): el payload y un intento de envío. Los reintentos están en app/services/kizuna_notifications.py.

El payload ya es el que definió KIZUNA. TODO(PENDIENTE-27): faltan la URL y la respuesta esperada (mientras tanto,
cualquier 2xx es éxito). Pasar al endpoint real solo debe tocar las variables de entorno.

Autenticación (KIZUNA_CALLBACK_AUTH_MODE, INTEGRACION_KIZUNA_OAUTH2.spec.md):
- header: KIZUNA_CALLBACK_AUTH_HEADER/_VALUE, como en M5 (header vacío = sin autenticación).
- oauth2: Bearer obtenido del servidor de autorización de KIZUNA (app/integrations/kizuna_oauth.py). Si el callback
  responde 401, se renueva el token y se reenvía una vez dentro del mismo intento.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import Settings
from app.integrations.kizuna_oauth import (
    KizunaTokenError,
    KizunaTokenProvider,
    KizunaTokenSource,
    get_kizuna_token_provider,
)
from app.models import Consent
from app.utils.datetimes import to_kizuna_iso

logger = logging.getLogger(__name__)

_ERROR_MAX_LENGTH = 1000
_BODY_MAX_LENGTH = 500


class KizunaConfigurationError(RuntimeError):
    """KIZUNA_CALLBACK_ENABLED=true con la configuración del callback incompleta: la aplicación no debe arrancar."""


def build_kizuna_payload(consent: Consent, zone: ZoneInfo) -> dict[str, Any]:
    """Respuesta final de un IdentifierId, con el contrato que definió KIZUNA.

    Única función que arma el payload. Cada registro (principal o copia vinculada, RN-04) se notifica con su propio
    IdentifierId. Los checks nunca van en null: lo que no se marcó y el marketing de un rechazo (RN-01) van en false.
    consentDateTime va con el offset de KIZUNA_TIMEZONE (America/Lima: -05:00).
    """
    deleted = consent.is_deleted == 1
    return {
        "IdentifierId": consent.transaction_id,
        "customerId": consent.customer_id,
        "dealerCode": consent.dealer_code,
        "consentDateTime": to_kizuna_iso(consent.responded_at, zone),
        "personalDataConsent": bool(consent.consent_personal_data),
        "marketingConsentYMDP": not deleted and bool(consent.consent_marketing_yamaha),
        "marketingConsentDEALER": not deleted and bool(consent.consent_marketing_dealer),
    }


@dataclass(frozen=True)
class KizunaSendResult:
    ok: bool
    # Error de red, timeout, 5xx o 429. Cualquier otra respuesta que no sea 2xx no se reintenta.
    retryable: bool
    http_status: int | None = None
    error: str | None = None
    # El fallo fue del pedido de token (oauth2), no del callback.
    error_from_token: bool = False


def is_retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


class KizunaClient:
    """Un intento de envío a KIZUNA (o al mock). No lanza excepciones: todo resultado queda en KizunaSendResult."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        tokens: KizunaTokenSource | None = None,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._headers = headers
        # Solo con KIZUNA_CALLBACK_AUTH_MODE=oauth2.
        self._tokens = tokens

    @classmethod
    def build_http_client(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=settings.kizuna_callback_timeout_seconds, transport=transport, follow_redirects=False
        )

    @classmethod
    def from_settings(
        cls,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        token_provider: KizunaTokenProvider | None = None,
    ) -> "KizunaClient":
        timeout = settings.kizuna_callback_timeout_seconds
        if settings.kizuna_callback_auth_mode == "oauth2":
            tokens = KizunaTokenSource(token_provider or get_kizuna_token_provider(), settings)
            return cls(client, timeout_seconds=timeout, headers={}, tokens=tokens)
        headers = {}
        if settings.kizuna_callback_auth_header.strip():
            headers[settings.kizuna_callback_auth_header.strip()] = (
                settings.kizuna_callback_auth_value.get_secret_value()
            )
        return cls(client, timeout_seconds=timeout, headers=headers)

    async def send(self, url: str, payload: dict[str, Any]) -> KizunaSendResult:
        tokens = self._tokens
        if tokens is None:
            return await self._post(url, payload, self._headers)

        result = await self._send_with_token(tokens, url, payload)
        if result.http_status != 401 or result.error_from_token:
            return result
        # 401 del callback: el token pudo vencer o revocarse antes de tiempo. Se renueva y se reenvía una sola vez,
        # dentro del mismo intento. Si vuelve 401, el resultado es definitivo (FALLIDO).
        logger.warning("Token de KIZUNA rechazado por el callback (401); se renueva")
        return await self._send_with_token(tokens, url, payload)

    async def _send_with_token(
        self, tokens: KizunaTokenSource, url: str, payload: dict[str, Any]
    ) -> KizunaSendResult:
        try:
            token = await tokens.get(self._client)
        except KizunaTokenError as exc:
            return KizunaSendResult(
                ok=False,
                retryable=exc.retryable,
                http_status=exc.http_status,
                error=_truncate(f"Token: {exc.error}"),
                error_from_token=True,
            )
        result = await self._post(url, payload, {**self._headers, "Authorization": f"Bearer {token}"})
        if result.http_status == 401:
            tokens.invalidate(token)
        return result

    async def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> KizunaSendResult:
        try:
            # El timeout de httpx es por operación (conexión, lectura...): este límite cubre el intento completo.
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._client.post(url, json=payload, headers=headers)
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
    - KIZUNA_CALLBACK_ENABLED=true y configuración incompleta → KizunaConfigurationError: la app no arranca (mismo
      criterio que la API Key y Twilio). Incompleta: sin KIZUNA_CALLBACK_URL; modo header con
      KIZUNA_CALLBACK_AUTH_HEADER sin su valor; modo oauth2 sin token URL, client_id, client_secret o scope, o con un
      header configurado; con APP_ENV=production, una URL que no sea https (sección 4.6 del spec OAuth2).
    """
    if not settings.kizuna_callback_enabled:
        logger.warning(
            "KIZUNA_CALLBACK_ENABLED=false: NO se envían respuestas a KIZUNA (las notificaciones quedan PENDIENTE)"
        )
        return

    problems = []
    if not settings.kizuna_callback_url.strip():
        problems.append("falta KIZUNA_CALLBACK_URL")
    if settings.kizuna_callback_auth_mode == "oauth2":
        problems += _oauth2_problems(settings)
    elif (
        settings.kizuna_callback_auth_header.strip()
        and not settings.kizuna_callback_auth_value.get_secret_value()
    ):
        problems.append("KIZUNA_CALLBACK_AUTH_HEADER está configurado pero falta KIZUNA_CALLBACK_AUTH_VALUE")
    if settings.app_env == "production":
        urls = {"KIZUNA_CALLBACK_URL": settings.kizuna_callback_url}
        if settings.kizuna_callback_auth_mode == "oauth2":
            urls["KIZUNA_OAUTH_TOKEN_URL"] = settings.kizuna_oauth_token_url
        problems += [
            f"{name} debe ser https en Producción"
            for name, url in urls.items()
            if url.strip() and not url.strip().lower().startswith("https://")
        ]
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


def _oauth2_problems(settings: Settings) -> list[str]:
    required = {
        "KIZUNA_OAUTH_TOKEN_URL": settings.kizuna_oauth_token_url,
        "KIZUNA_OAUTH_CLIENT_ID": settings.kizuna_oauth_client_id,
        "KIZUNA_OAUTH_CLIENT_SECRET": settings.kizuna_oauth_client_secret.get_secret_value(),
        "KIZUNA_OAUTH_SCOPE": settings.kizuna_oauth_scope,
    }
    problems = [
        f"falta {name} (KIZUNA_CALLBACK_AUTH_MODE=oauth2)" for name, value in required.items() if not value
    ]
    if settings.kizuna_callback_auth_header.strip():
        problems.append("KIZUNA_CALLBACK_AUTH_HEADER debe quedar vacío con KIZUNA_CALLBACK_AUTH_MODE=oauth2")
    return problems
