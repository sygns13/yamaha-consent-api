"""Catálogo Ubigeo INEI (BACKOFFICE_DEALERS.spec.md, sección 6.7). Módulo `DEALERS`: alimenta los selects encadenados
del formulario de dealers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.core.backoffice_security import BACKOFFICE_PREFIX, SessionDep, require_module
from app.models.role import ModuleCode
from app.schemas.backoffice.auth import CodeName
from app.schemas.backoffice.common import error_responses
from app.services.backoffice import ubigeo as ubigeo_service
from app.services.backoffice.sessions import AuthenticatedSession

router = APIRouter(prefix=f"{BACKOFFICE_PREFIX}/ubigeo", tags=["backoffice-catalogs"])

DealersModule = Annotated[AuthenticatedSession, Depends(require_module(ModuleCode.DEALERS))]
# Sin validar el formato: un código mal formado tampoco existe (404).
DepartmentCode = Annotated[str, Path(alias="departmentCode")]
ProvinceCode = Annotated[str, Path(alias="provinceCode")]


@router.get(
    "/departments",
    response_model=list[CodeName],
    summary="Departamentos",
    responses=error_responses(401, 403),
)
async def list_departments(_current: DealersModule, db: SessionDep) -> list[CodeName]:
    """Ordenados por nombre."""
    return await ubigeo_service.list_departments(db)


@router.get(
    "/departments/{departmentCode}/provinces",
    response_model=list[CodeName],
    summary="Provincias de un departamento",
    responses=error_responses(401, 403, 404),
)
async def list_provinces(
    department_code: DepartmentCode, _current: DealersModule, db: SessionDep
) -> list[CodeName]:
    """Ordenadas por nombre. Departamento inexistente → 404 NOT_FOUND."""
    return await ubigeo_service.list_provinces(db, department_code)


@router.get(
    "/provinces/{provinceCode}/districts",
    response_model=list[CodeName],
    summary="Distritos de una provincia",
    responses=error_responses(401, 403, 404),
)
async def list_districts(
    province_code: ProvinceCode, _current: DealersModule, db: SessionDep
) -> list[CodeName]:
    """Ordenados por nombre. Provincia inexistente → 404 NOT_FOUND."""
    return await ubigeo_service.list_districts(db, province_code)
