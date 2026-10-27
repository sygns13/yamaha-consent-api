from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import NotificationStatus, notification_status_enum
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class KizunaNotification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Outbox de notificaciones a KIZUNA (o al mock)."""

    __tablename__ = "kizuna_notifications"

    transaction_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("consents.transaction_id"), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Indexado para reprocesar las notificaciones PENDIENTE al arrancar.
    status: Mapped[NotificationStatus] = mapped_column(
        notification_status_enum,
        nullable=False,
        index=True,
        default=NotificationStatus.PENDIENTE,
        server_default=NotificationStatus.PENDIENTE.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Reclamo del envío: una sola instancia envía cada notificación. Se marca al reclamarla (SELECT ... FOR UPDATE
    # SKIP LOCKED) y se renueva en cada intento. Si pasan KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES sin renovarse (la
    # instancia se reinició a mitad de los reintentos), el reproceso al arrancar la vuelve a reclamar.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
