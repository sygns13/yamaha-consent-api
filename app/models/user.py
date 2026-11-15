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
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

ACTIVE_WHERE = text("is_deleted = 0")


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
    # Concurrencia optimista (propuesta): cada actualización la incrementa.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    is_deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Autoría; nulos cuando actúa la CLI.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
