import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.dialects.postgresql import CHAR, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class SessionRevokedReason(enum.StrEnum):
    """Valores de user_sessions.revoked_reason (varchar, no enum de PostgreSQL)."""

    LOGOUT = "LOGOUT"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    USER_DELETED = "USER_DELETED"
    ADMIN_PASSWORD_RESET = "ADMIN_PASSWORD_RESET"


class UserSession(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Sesión del BackOffice (cookie `__session`). Solo se guarda el SHA-256 del token; el token nunca."""

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    # Se renueva como máximo una vez por minuto.
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # created_at + BACKOFFICE_SESSION_MAX_HOURS (vencimiento absoluto).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Primera IP de X-Forwarded-For.
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
