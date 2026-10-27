"""Envío del WhatsApp con el enlace de la landing.

TODO(M3): agregar TwilioWhatsAppSender (Content API, plantilla Call to Action) y devolverlo en get_whatsapp_sender.
Mientras tanto se usa FakeWhatsAppSender, que no envía nada.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

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


def get_whatsapp_sender() -> WhatsAppSender:
    """Dependencia de FastAPI. Los tests la reemplazan con app.dependency_overrides."""
    return _fake_sender
