"""Envío del WhatsApp por Twilio (M3): Content API con la plantilla Call to Action "Revisar autorizaciones".

La plantilla tiene el dominio de la landing fijo en el botón (`https://<DOMINIO_LANDING>/?t={{1}}`); aquí solo se
envía la variable {{1}} = token. Un fallo de Twilio nunca lanza excepción hacia el servicio: se devuelve como
WhatsAppSendResult(status=failed, ...) y queda en whatsapp_messages (M2, paso 6).

El token es un secreto: no se loguea. El SDK de Twilio no registra el body de las peticiones, y su logger
(twilio.http_client) queda en WARNING (app/core/logging.py).
"""

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from requests import RequestException
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.core.config import Settings
from app.integrations.whatsapp import WhatsAppSendResult
from app.models.enums import WhatsappStatus
from app.utils.masking import mask_phone

logger = logging.getLogger(__name__)

# Estados que puede devolver Twilio al crear el mensaje y que whatsapp_status no tiene: el mensaje está en camino.
_IN_PROGRESS_STATUSES = {"accepted", "scheduled", "sending"}
_ERROR_MESSAGE_MAX_LENGTH = 1000
# El SDK antepone este texto al mensaje de error de Twilio; se guarda el mensaje original.
_SDK_ERROR_PREFIX = "Unable to create record: "


class TwilioConfigurationError(RuntimeError):
    """TWILIO_SEND_ENABLED=true con la configuración de Twilio incompleta: la aplicación no debe arrancar."""


class TwilioWhatsAppSender:
    """WhatsAppSender real. `client` es un twilio.rest.Client (los tests le pasan un HttpClient simulado)."""

    def __init__(self, client: Client, *, from_: str, content_sid: str, status_callback: str | None) -> None:
        self._client = client
        self._from = from_
        self._content_sid = content_sid
        self._status_callback = status_callback or None

    @classmethod
    def from_settings(cls, settings: Settings) -> "TwilioWhatsAppSender":
        http_client = TwilioHttpClient(timeout=settings.twilio_timeout_seconds)
        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
            http_client=http_client,
        )
        return cls(
            client,
            from_=settings.twilio_whatsapp_from,
            content_sid=settings.twilio_content_sid,
            status_callback=settings.twilio_status_callback_url,
        )

    async def send_consent_link(self, *, to: str, token: str, transaction_id: str) -> WhatsAppSendResult:
        params: dict[str, Any] = {
            "from_": self._from,
            "to": to,
            "content_sid": self._content_sid,
            "content_variables": json.dumps({"1": token}),
        }
        if self._status_callback:
            params["status_callback"] = self._status_callback

        log_extra = {"transaction_id": transaction_id, "to": mask_phone(to)}
        try:
            # El SDK es síncrono: se ejecuta en un hilo para no bloquear el event loop.
            message = await asyncio.to_thread(self._client.messages.create, **params)
        except TwilioRestException as exc:
            # Twilio respondió con error (p. ej. 21211 número inválido, 63016 fuera de la ventana de 24 h).
            logger.warning(
                "Twilio rechazó el envío del WhatsApp",
                extra={**log_extra, "error_code": exc.code, "http_status": exc.status},
            )
            return WhatsAppSendResult(
                status=WhatsappStatus.FAILED,
                error_code=str(exc.code) if exc.code is not None else None,
                error_message=_truncate(exc.msg.removeprefix(_SDK_ERROR_PREFIX)),
            )
        except (TwilioException, RequestException) as exc:
            # No se llegó a Twilio (red, timeout, credenciales mal formadas...): sin SID ni código de Twilio.
            logger.error(
                "No se pudo enviar el WhatsApp por Twilio",
                extra={**log_extra, "error_type": type(exc).__name__},
            )
            return WhatsAppSendResult(
                status=WhatsappStatus.FAILED, error_message=_truncate(f"{type(exc).__name__}: {exc}")
            )

        status = _map_status(message.status)
        error_code = str(message.error_code) if message.error_code is not None else None
        logger.info(
            "WhatsApp enviado a Twilio",
            extra={**log_extra, "message_sid": message.sid, "status": status.value, "error_code": error_code},
        )
        return WhatsAppSendResult(
            status=status,
            message_sid=message.sid,
            error_code=error_code,
            error_message=_truncate(message.error_message),
        )


def parse_twilio_status(raw: object) -> WhatsappStatus | None:
    """Estado de Twilio → whatsapp_status. `accepted`, `scheduled` y `sending` → queued. Desconocido → None."""
    value = str(getattr(raw, "value", raw) or "").strip().lower()
    if value in _IN_PROGRESS_STATUSES:
        return WhatsappStatus.QUEUED
    try:
        return WhatsappStatus(value)
    except ValueError:
        return None


def _map_status(raw: object) -> WhatsappStatus:
    status = parse_twilio_status(raw)
    if status is None:
        logger.warning(
            "Estado de Twilio desconocido al crear el mensaje: se registra como queued",
            extra={"twilio_status": str(raw)},
        )
        return WhatsappStatus.QUEUED
    return status


def is_valid_twilio_signature(
    *, auth_token: str, signature: str, urls: Iterable[str], params: Mapping[str, str]
) -> bool:
    """Valida X-Twilio-Signature con RequestValidator contra alguna de las URLs candidatas.

    Twilio firma la URL exacta a la que llamó (la de status_callback), y RequestValidator ya prueba las variantes
    con y sin puerto. Sin auth token o sin firma no hay nada que validar: inválida.
    """
    if not auth_token or not signature:
        return False
    validator = RequestValidator(auth_token)
    return any(validator.validate(url, dict(params), signature) for url in urls if url)


def _truncate(text: str | None) -> str | None:
    return text[:_ERROR_MESSAGE_MAX_LENGTH] if text else None


def check_twilio_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app.

    - TWILIO_SEND_ENABLED=false → log WARNING (no se envían WhatsApp reales).
    - TWILIO_SEND_ENABLED=true con datos faltantes o mal formados → TwilioConfigurationError: la app no arranca.
    - Sin TWILIO_STATUS_CALLBACK_URL → log WARNING: los estados no avanzarán más allá de `queued`.
    - TWILIO_SIGNATURE_VALIDATION_ENABLED=false → log WARNING (el webhook acepta callbacks sin firma; solo local).
    - Validación activa sin TWILIO_AUTH_TOKEN → log WARNING: el webhook rechazará todos los callbacks (403).
    """
    if not settings.twilio_signature_validation_enabled:
        logger.warning(
            "TWILIO_SIGNATURE_VALIDATION_ENABLED=false: el webhook de Twilio NO valida la firma (solo para local)"
        )
    elif not settings.twilio_auth_token.get_secret_value().strip():
        logger.warning("TWILIO_AUTH_TOKEN vacío: el webhook de Twilio rechazará todos los callbacks (403)")

    if not settings.twilio_send_enabled:
        logger.warning("TWILIO_SEND_ENABLED=false: los WhatsApp NO se envían (solo se registran en el log)")
        return

    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token.get_secret_value()),
            ("TWILIO_WHATSAPP_FROM", settings.twilio_whatsapp_from),
            ("TWILIO_CONTENT_SID", settings.twilio_content_sid),
        )
        if not value.strip()
    ]
    problems = [f"faltan {', '.join(missing)}"] if missing else []
    if settings.twilio_whatsapp_from.strip() and not settings.twilio_whatsapp_from.startswith("whatsapp:+"):
        problems.append("TWILIO_WHATSAPP_FROM debe tener el formato whatsapp:+<número>")
    if problems:
        message = (
            f"TWILIO_SEND_ENABLED=true pero la configuración de Twilio está incompleta: {'; '.join(problems)}"
        )
        logger.critical(message)
        raise TwilioConfigurationError(message)

    if not settings.twilio_status_callback_url.strip():
        logger.warning(
            "TWILIO_STATUS_CALLBACK_URL vacía: Twilio no informará los estados de los mensajes (quedarán en queued)"
        )
