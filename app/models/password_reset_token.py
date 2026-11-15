import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import CHAR, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class PasswordResetToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Enlace de restablecimiento de contraseña, de un solo uso. Solo se guarda el SHA-256 del token."""

    __tablename__ = "password_reset_tokens"
    # Para el límite de enlaces por hora (BACKOFFICE_PASSWORD_RESET_MAX_PER_HOUR).
    __table_args__ = (Index(None, "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    # created_at + BACKOFFICE_PASSWORD_RESET_TTL_MINUTES.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Se marca al pedir un enlace nuevo o al cambiar la contraseña.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    requested_user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
