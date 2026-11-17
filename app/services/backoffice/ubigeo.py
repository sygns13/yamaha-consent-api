"""Catálogo Ubigeo INEI (BACKOFFICE_DEALERS.spec.md, sección 6.7): departamento → provincia → distrito, ordenados por
nombre. Un código inexistente (también uno mal formado) → 404 NOT_FOUND."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.backoffice_security import not_found
from app.models.ubigeo import UbigeoDepartment, UbigeoDistrict, UbigeoProvince
from app.schemas.backoffice.auth import CodeName

DEPARTMENT_NOT_FOUND = "No existe el departamento."
PROVINCE_NOT_FOUND = "No existe la provincia."


async def list_departments(db: AsyncSession) -> list[CodeName]:
    rows = await db.execute(
        select(UbigeoDepartment.code, UbigeoDepartment.name).order_by(
            UbigeoDepartment.name, UbigeoDepartment.code
        )
    )
    return [CodeName(code=row.code, name=row.name) for row in rows]


async def list_provinces(db: AsyncSession, department_code: str) -> list[CodeName]:
    if await db.get(UbigeoDepartment, department_code) is None:
        raise not_found(DEPARTMENT_NOT_FOUND)
    rows = await db.execute(
        select(UbigeoProvince.code, UbigeoProvince.name)
        .where(UbigeoProvince.department_code == department_code)
        .order_by(UbigeoProvince.name, UbigeoProvince.code)
    )
    return [CodeName(code=row.code, name=row.name) for row in rows]


async def list_districts(db: AsyncSession, province_code: str) -> list[CodeName]:
    if await db.get(UbigeoProvince, province_code) is None:
        raise not_found(PROVINCE_NOT_FOUND)
    rows = await db.execute(
        select(UbigeoDistrict.code, UbigeoDistrict.name)
        .where(UbigeoDistrict.province_code == province_code)
        .order_by(UbigeoDistrict.name, UbigeoDistrict.code)
    )
    return [CodeName(code=row.code, name=row.name) for row in rows]
