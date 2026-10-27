"""Callbacks de estado de Twilio (M3): whatsapp_message_events y estado de whatsapp_messages.

- **Idempotente:** un evento por mensaje y estado (restricción única + INSERT ... ON CONFLICT DO NOTHING). Un
  callback repetido no crea otro evento ni vuelve a modificar el mensaje, aunque lleguen dos a la vez.
- **El estado no retrocede:** Twilio puede entregar los callbacks desordenados (p. ej. `read` antes que `delivered`).
  Todos se guardan como eventos, pero whatsapp_messages.status solo cambia si el estado nuevo está más avanzado.
"""

import enum
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.twilio_client import parse_twilio_status
from app.models import WhatsappMessage, WhatsappMessageEvent, WhatsappStatus
from app.schemas.twilio import TwilioStatusCallback

logger = logging.getLogger(__name__)

_ERROR_MESSAGE_MAX_LENGTH = 1000

# Avance de un mensaje. failed y undelivered son finales y ocupan el lugar de delivered: un mensaje entregado no
# pasa a fallido por un callback atrasado, y uno fallido no pasa a entregado. `read` es lo más avanzado.
STATUS_RANK = {
    WhatsappStatus.QUEUED: 0,
    WhatsappStatus.SENT: 1,
    WhatsappStatus.DELIVERED: 2,
    WhatsappStatus.UNDELIVERED: 2,
    WhatsappStatus.FAILED: 2,
    WhatsappStatus.READ: 3,
}


class CallbackOutcome(enum.StrEnum):
    UPDATED = "UPDATED"  # evento nuevo y el mensaje cambió de estado
    RECORDED = "RECORDED"  # evento nuevo, pero el estado no avanza (callback atrasado o igual)
    DUPLICATE = "DUPLICATE"  # callback repetido: no se hizo nada
    UNKNOWN_MESSAGE = "UNKNOWN_MESSAGE"  # MessageSid que no es de este sistema
    UNKNOWN_STATUS = "UNKNOWN_STATUS"  # MessageStatus que whatsapp_status no contempla


async def record_status_callback(
    session: AsyncSession, callback: TwilioStatusCallback, raw_payload: dict[str, Any]
) -> CallbackOutcome:
    """Registra el callback en una transacción. raw_payload se guarda tal cual (incluye el teléfono: no se loguea)."""
    log_extra = {"message_sid": callback.message_sid, "twilio_status": callback.message_status}

    status = parse_twilio_status(callback.message_status)
    if status is None:
        logger.warning("Callback de Twilio con un estado desconocido: se ignora", extra=log_extra)
        return CallbackOutcome.UNKNOWN_STATUS

    async with session.begin():
        message = await session.scalar(
            select(WhatsappMessage)
            .where(WhatsappMessage.twilio_message_sid == callback.message_sid)
            .with_for_update()
        )
        if message is None:
            logger.warning("Callback de Twilio para un mensaje que no existe: se ignora", extra=log_extra)
            return CallbackOutcome.UNKNOWN_MESSAGE

        inserted = await session.scalar(
            insert(WhatsappMessageEvent)
            .values(
                message_id=message.id, status=status, error_code=callback.error_code, raw_payload=raw_payload
            )
            .on_conflict_do_nothing(index_elements=["message_id", "status"])
            .returning(WhatsappMessageEvent.id)
        )
        if inserted is None:
            logger.info("Callback de Twilio repetido: se ignora", extra=log_extra)
            return CallbackOutcome.DUPLICATE

        if STATUS_RANK[status] <= STATUS_RANK[message.status]:
            logger.info(
                "Callback de Twilio registrado; el estado del mensaje no cambia",
                extra={**log_extra, "current_status": message.status.value},
            )
            return CallbackOutcome.RECORDED

        previous = message.status
        message.status = status
        message.error_code = callback.error_code
        message.error_message = (
            callback.error_message[:_ERROR_MESSAGE_MAX_LENGTH] if callback.error_message else None
        )
        logger.info(
            "Estado del WhatsApp actualizado",
            extra={
                **log_extra,
                "transaction_id": message.transaction_id,
                "previous_status": previous.value,
                "status": status.value,
                "error_code": callback.error_code,
            },
        )
        return CallbackOutcome.UPDATED
