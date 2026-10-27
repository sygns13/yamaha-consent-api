from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

ACTION_CREATED = "CREATED"
ACTION_UPDATED = "UPDATED"


class ConsentRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Historial de solicitudes recibidas desde KIZUNA. No guarda el payload para no duplicar datos personales."""

    __tablename__ = "consent_requests"
    __table_args__ = (CheckConstraint("action IN ('CREATED', 'UPDATED')", name="action_valid"),)

    # ID recibido en la solicitud. En RN-04 puede ser un ID nuevo que no existe en consents.
    transaction_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Registro afectado (el creado o el registro activo actualizado).
    consent_transaction_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("consents.transaction_id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
