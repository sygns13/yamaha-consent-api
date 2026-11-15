"""Catálogo Ubigeo INEI (departamento → provincia → distrito). PK = código INEI (no RENIEC).

Lo carga la migración `ubigeo_inei_seed` desde `alembic/data/ubigeo_inei.csv` (sección 4.8 del spec del BackOffice;
fuente en `alembic/data/UBIGEO_FUENTE.md`). Nombres tal como vienen en la fuente, con trim y NFC.
"""

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UbigeoDepartment(Base):
    __tablename__ = "ubigeo_departments"
    __table_args__ = (CheckConstraint("code ~ '^[0-9]{2}$'", name="code_format"),)

    code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)


class UbigeoProvince(Base):
    __tablename__ = "ubigeo_provinces"
    __table_args__ = (
        CheckConstraint("code ~ '^[0-9]{4}$'", name="code_format"),
        CheckConstraint("left(code, 2) = department_code", name="code_in_department"),
    )

    code: Mapped[str] = mapped_column(CHAR(4), primary_key=True)
    department_code: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("ubigeo_departments.code"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class UbigeoDistrict(Base):
    __tablename__ = "ubigeo_districts"
    __table_args__ = (
        CheckConstraint("code ~ '^[0-9]{6}$'", name="code_format"),
        CheckConstraint("left(code, 4) = province_code", name="code_in_province"),
    )

    code: Mapped[str] = mapped_column(CHAR(6), primary_key=True)
    province_code: Mapped[str] = mapped_column(
        CHAR(4), ForeignKey("ubigeo_provinces.code"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
