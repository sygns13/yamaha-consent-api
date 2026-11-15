import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import UUIDPrimaryKeyMixin


class AuditAction(enum.StrEnum):
    """Catálogo de acciones de la bitácora (sección 5.10). Se guarda como varchar: agregar una acción no requiere
    migración."""

    AUTH_LOGIN_SUCCEEDED = "AUTH_LOGIN_SUCCEEDED"
    AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
    AUTH_ACCOUNT_LOCKED = "AUTH_ACCOUNT_LOCKED"
    AUTH_LOGOUT = "AUTH_LOGOUT"
    AUTH_PASSWORD_CHANGED = "AUTH_PASSWORD_CHANGED"
    AUTH_PASSWORD_RESET_REQUESTED = "AUTH_PASSWORD_RESET_REQUESTED"
    AUTH_PASSWORD_RESET_COMPLETED = "AUTH_PASSWORD_RESET_COMPLETED"
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_ACTIVATED = "USER_ACTIVATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    USER_DELETED = "USER_DELETED"
    USER_UNLOCKED = "USER_UNLOCKED"
    USER_PASSWORD_RESET_BY_ADMIN = "USER_PASSWORD_RESET_BY_ADMIN"
    PROFILE_UPDATED = "PROFILE_UPDATED"
    DEALER_CREATED = "DEALER_CREATED"
    DEALER_UPDATED = "DEALER_UPDATED"
    DEALER_DELETED = "DEALER_DELETED"
    DEALERS_EXPORTED = "DEALERS_EXPORTED"
    SYSTEM_PURGE = "SYSTEM_PURGE"


class AuditEntityType(enum.StrEnum):
    USER = "USER"
    DEALER = "DEALER"


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """Bitácora de auditoría: **solo inserción**.

    La aplicación nunca actualiza ni borra filas (en cada entorno se ejecuta `REVOKE UPDATE, DELETE ON audit_logs FROM
    consent_app`). Solo la depuración por antigüedad de la CLI, con el usuario dueño, borra filas (sección 5.13).
    `details` nunca lleva contraseñas, hashes ni tokens.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index(None, "actor_user_id", "occurred_at"),
        Index(None, "entity_type", "entity_id"),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    # Nulo en acciones anónimas (login de un usuario inexistente, solicitud de restablecimiento) o de la CLI.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # UUID del usuario o dealer afectado.
    entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # X-Request-ID de la petición.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
