"""Roles, módulos y permisos del BackOffice (sección 3 y 4.3 del spec del BackOffice). Catálogos con PK natural.

Las filas se insertan en la migración `backoffice_access_dealers`; no hay pantalla para editarlas (sección 13, F7).
"""

import enum

from sqlalchemy import ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, TimestampMixin


class RoleCode(enum.StrEnum):
    ADMIN = "ADMIN"
    OPERADOR = "OPERADOR"


class ModuleCode(enum.StrEnum):
    DEALERS = "DEALERS"
    USUARIOS = "USUARIOS"
    AUDITORIA = "AUDITORIA"


class Role(TimestampMixin, Base):
    """Tipo de usuario: ADMIN (todos los módulos) u OPERADOR (solo Dealers)."""

    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(250), nullable=True)


class BackofficeModule(TimestampMixin, Base):
    """Módulo con permiso propio. `description` es el texto de su tarjeta en Inicio (Anexo A.2)."""

    __tablename__ = "backoffice_modules"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(250), nullable=True)
    # Orden en el menú: DEALERS 10, USUARIOS 20, AUDITORIA 30.
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class RoleModule(CreatedAtMixin, Base):
    """Permiso rol → módulo. Se lee en cada petición: un cambio de rol aplica en la siguiente."""

    __tablename__ = "role_modules"

    role_code: Mapped[str] = mapped_column(String(20), ForeignKey("roles.code"), primary_key=True)
    module_code: Mapped[str] = mapped_column(
        String(30), ForeignKey("backoffice_modules.code"), primary_key=True
    )
