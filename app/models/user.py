import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

ACTIVE_WHERE = text("is_deleted = 0")


class UserStatus(enum.StrEnum):
    """Estado derivado (sección 6.5): INACTIVO si `is_active = false`; si no, BLOQUEADO si `locked_until > now`; si
    no, ACTIVO. No se guarda."""

    ACTIVO = "ACTIVO"
    INACTIVO = "INACTIVO"
    BLOQUEADO = "BLOQUEADO"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Usuario del BackOffice (no confundir con el cliente final de consents).

    Unicidad de `username`, `email` y `dni` solo entre usuarios vigentes (índices únicos parciales `WHERE
    is_deleted = 0`): un usuario eliminado libera sus valores.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("dni ~ '^[0-9]{8}$'", name="dni_format"),
        CheckConstraint("username ~ '^[a-z][a-z0-9._-]{3,29}$'", name="username_format"),
        CheckConstraint("phone IS NULL OR phone ~ '^[0-9]{9}$'", name="phone_format"),
        CheckConstraint("failed_login_attempts >= 0", name="failed_login_attempts_non_negative"),
        CheckConstraint("is_deleted IN (0, 1)", name="is_deleted_bool"),
        Index("uq_users_active_username", "username", unique=True, postgresql_where=ACTIVE_WHERE),
        Index("uq_users_active_email", "email", unique=True, postgresql_where=ACTIVE_WHERE),
        Index("uq_users_active_dni", "dni", unique=True, postgresql_where=ACTIVE_WHERE),
    )

    role_code: Mapped[str] = mapped_column(String(20), ForeignKey("roles.code"), nullable=False, index=True)
    dni: Mapped[str] = mapped_column(CHAR(8), nullable=False)
    # Trim, espacios colapsados y NFC; se conservan mayúsculas y minúsculas.
    first_names: Mapped[str] = mapped_column(String(100), nullable=False)
    last_names: Mapped[str] = mapped_column(String(100), nullable=False)
    # En minúsculas. Inmutable.
    username: Mapped[str] = mapped_column(String(30), nullable=False)
    # Trim + minúsculas. Canal de recuperación de contraseña.
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    phone: Mapped[str | None] = mapped_column(CHAR(9), nullable=True)
    # Argon2id en formato PHC ($argon2id$...). Nunca se loguea.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    failed_login_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Concurrencia optimista (propuesta). Sube solo al editar los datos del formulario: PUT /users y PATCH /profile
    # (sección 4.1, decisión de la sesión 3). El login, el bloqueo y las acciones no la tocan: no generan
    # VERSION_CONFLICT falsos.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    is_deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Autoría; nulos cuando actúa la CLI.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    # Reemplaza el de TimestampMixin **sin** `onupdate` (sección 4.1, decisión de la sesión 3): `updated_at` y
    # `updated_by` cambian solo con una acción de una persona sobre la ficha (PUT, PATCH /profile, activar,
    # desactivar, desbloquear, contraseña temporal, eliminar), que los fija de forma explícita. El login, los intentos
    # fallidos, el bloqueo automático y los cambios de contraseña del propio usuario no los tocan. No cambia el esquema
    # (onupdate solo existe en Python).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), sort_order=10
    )
