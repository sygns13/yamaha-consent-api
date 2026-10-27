import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import WhatsappStatus, whatsapp_status_enum
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class WhatsappMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Mensaje de WhatsApp enviado por Twilio. El borrado lógico (RN-01) no afecta esta tabla."""

    __tablename__ = "whatsapp_messages"

    transaction_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("consents.transaction_id"), nullable=False, index=True
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("consent_links.id"), nullable=False, index=True
    )
    # Nulo si el envío falló antes de llegar a Twilio.
    twilio_message_sid: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # whatsapp:+51XXXXXXXXX (21 caracteres). La spec indica varchar(20), que no alcanza; 30 cubre E.164 completo.
    to_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    content_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    # Último estado conocido.
    status: Mapped[WhatsappStatus] = mapped_column(whatsapp_status_enum, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class WhatsappMessageEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Cada callback de estado recibido de Twilio.

    Un evento por mensaje y estado: un callback repetido (mismo MessageSid y MessageStatus) no se vuelve a guardar
    (idempotencia, M3). La restricción única lo garantiza también ante callbacks concurrentes.
    """

    __tablename__ = "whatsapp_message_events"
    __table_args__ = (UniqueConstraint("message_id", "status"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("whatsapp_messages.id"), nullable=False, index=True
    )
    status: Mapped[WhatsappStatus] = mapped_column(whatsapp_status_enum, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
