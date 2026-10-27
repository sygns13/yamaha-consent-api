"""Envío del WhatsApp con el enlace de la landing.

- TWILIO_SEND_ENABLED=true  → TwilioWhatsAppSender (app/integrations/twilio_client.py): envío real.
- TWILIO_SEND_ENABLED=false → FakeWhatsAppSender: no envía nada, solo registra en el log (desarrollo).
"""

import logging
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.models.enums import WhatsappStatus
from app.utils.masking import mask_phone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhatsAppSendResult:
    """Resultado del envío. Un fallo se informa aquí (status failed/undelivered), no con una excepción."""

    status: WhatsappStatus
    # Nulo si el envío falló antes de llegar a Twilio.
    message_sid: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class WhatsAppSender(Protocol):
    async def send_consent_link(self, *, to: str, token: str, transaction_id: str) -> WhatsAppSendResult:
        """Envía la plantilla con el botón hacia {LANDING_BASE_URL}/?t={token}.

        `to` va en formato whatsapp:+51XXXXXXXXX. El token es un secreto: nunca se loguea.
        """
        ...


class FakeWhatsAppSender:
    """No envía nada: solo registra en el log (teléfono enmascarado, sin token) y responde "queued"."""

    async def send_consent_link(self, *, to: str, token: str, transaction_id: str) -> WhatsAppSendResult:
        logger.info(
            "WhatsApp SIMULADO (no se envió ningún mensaje)",
            extra={"transaction_id": transaction_id, "to": mask_phone(to)},
        )
        return WhatsAppSendResult(status=WhatsappStatus.QUEUED)


_fake_sender = FakeWhatsAppSender()
# Un TwilioWhatsAppSender por configuración (reutiliza las conexiones HTTP del cliente de Twilio).
_twilio_senders: dict[tuple[object, ...], WhatsAppSender] = {}


def get_whatsapp_sender(settings: Annotated[Settings, Depends(get_settings)]) -> WhatsAppSender:
    """Dependencia de FastAPI según TWILIO_SEND_ENABLED. Los tests la reemplazan con app.dependency_overrides."""
    if not settings.twilio_send_enabled:
        return _fake_sender

    # Import diferido: twilio_client importa este módulo.
    from app.integrations.twilio_client import TwilioWhatsAppSender

    key = (
        settings.twilio_account_sid,
        settings.twilio_auth_token.get_secret_value(),
        settings.twilio_whatsapp_from,
        settings.twilio_content_sid,
        settings.twilio_status_callback_url,
        settings.twilio_timeout_seconds,
    )
    if key not in _twilio_senders:
        _twilio_senders[key] = TwilioWhatsAppSender.from_settings(settings)
    return _twilio_senders[key]
