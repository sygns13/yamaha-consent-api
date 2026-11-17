"""Dealers del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.6). Módulo `DEALERS` (Administrador y Operador).

`/dealers/lookup` y `/dealers/export` se declaran antes de `/dealers/{dealerId}`. Las respuestas de las mutaciones se
arman leyendo de nuevo el dealer después del commit.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.backoffice_security import BACKOFFICE_PREFIX, SessionDep, SettingsDep, not_found, require_module
from app.models.role import ModuleCode
from app.schemas.backoffice.common import Page, error_responses
from app.schemas.backoffice.dealers import (
    DealerCreateRequest,
    DealerDetail,
    DealerFilterQuery,
    DealerListQuery,
    DealerLookupQuery,
    DealerUpdateRequest,
)
from app.services.backoffice import dealers as dealers_service
from app.services.backoffice import exports
from app.services.backoffice.audit import AuditContextDep
from app.services.backoffice.sessions import AuthenticatedSession

router = APIRouter(prefix=f"{BACKOFFICE_PREFIX}/dealers", tags=["backoffice-dealers"])

DealersModule = Annotated[AuthenticatedSession, Depends(require_module(ModuleCode.DEALERS))]
DealerId = Annotated[uuid.UUID, Path(alias="dealerId")]

# 401 sin sesión, 403 sin el módulo (o con cambio de contraseña pendiente).
_SESSION_ERRORS = (401, 403)


@router.get(
    "",
    response_model=Page[DealerDetail],
    summary="Lista de dealers",
    responses=error_responses(*_SESSION_ERRORS),
)
async def list_dealers(
    query: Annotated[DealerListQuery, Query()], _current: DealersModule, db: SessionDep, settings: SettingsDep
) -> Page[DealerDetail]:
    """Excluye los eliminados. `q`: código, razón social, RUC o persona de contacto. Filtros `departmentCode`,
    `provinceCode`, `districtUbigeo`. `sortBy`: dealerCode (default), businessName, ruc, createdAt, updatedAt."""
    items, total = await dealers_service.list_dealers(db, query, zone=settings.kizuna_zone)
    return Page[DealerDetail](items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DealerDetail,
    summary="Registrar un dealer",
    responses=error_responses(*_SESSION_ERRORS, 409, 422),
)
async def create_dealer(
    body: DealerCreateRequest,
    current: DealersModule,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
) -> DealerDetail:
    """409 DUPLICATE (`dealerCode` de un dealer vigente, sin distinguir mayúsculas); 422 DISTRICT_NOT_FOUND."""
    dealer_id = await dealers_service.create_dealer(db, body, actor_user_id=current.user.id, context=context)
    return await dealers_service.get_dealer_detail(db, dealer_id, zone=settings.kizuna_zone)


@router.get(
    "/lookup",
    response_model=DealerDetail,
    summary="Buscar el dealer vigente por código",
    responses=error_responses(*_SESSION_ERRORS, 404),
)
async def lookup_dealer(
    query: Annotated[DealerLookupQuery, Query()],
    _current: DealersModule,
    db: SessionDep,
    settings: SettingsDep,
) -> DealerDetail:
    """`dealerCode` sin distinguir mayúsculas (trim). Eliminado o inexistente → 404 NOT_FOUND."""
    dealer = await dealers_service.get_active_dealer_by_code(db, query.dealer_code)
    if dealer is None:
        raise not_found(dealers_service.DEALER_NOT_FOUND)
    return await dealers_service.get_dealer_detail(db, dealer.id, zone=settings.kizuna_zone)


@router.get(
    "/export",
    response_class=Response,
    summary="Exportar dealers a Excel",
    responses={
        200: {"content": {exports.XLSX_MEDIA_TYPE: {}}, "description": "Archivo .xlsx"},
        **error_responses(*_SESSION_ERRORS),
    },
)
async def export_dealers(
    query: Annotated[DealerFilterQuery, Query()],
    current: DealersModule,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
) -> Response:
    """Mismos filtros y orden que la lista, sin paginación. Archivo `dealers_AAAAMMDD_HHMM.xlsx` (hora de Lima)."""
    content, filename = await exports.export_dealers(
        db, query, actor_user_id=current.user.id, context=context, zone=settings.kizuna_zone
    )
    return Response(
        content=content,
        media_type=exports.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{dealerId}",
    response_model=DealerDetail,
    summary="Detalle de un dealer",
    responses=error_responses(*_SESSION_ERRORS, 404),
)
async def get_dealer(
    dealer_id: DealerId, _current: DealersModule, db: SessionDep, settings: SettingsDep
) -> DealerDetail:
    return await dealers_service.get_dealer_detail(db, dealer_id, zone=settings.kizuna_zone)


@router.put(
    "/{dealerId}",
    response_model=DealerDetail,
    summary="Editar un dealer",
    responses=error_responses(*_SESSION_ERRORS, 404, 409, 422),
)
async def update_dealer(
    dealer_id: DealerId,
    body: DealerUpdateRequest,
    current: DealersModule,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
) -> DealerDetail:
    """Reemplazo completo de los campos editables (`dealerCode` no se edita). 409 VERSION_CONFLICT; 422
    DISTRICT_NOT_FOUND."""
    await dealers_service.update_dealer(db, dealer_id, body, actor_user_id=current.user.id, context=context)
    return await dealers_service.get_dealer_detail(db, dealer_id, zone=settings.kizuna_zone)


@router.delete(
    "/{dealerId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un dealer",
    responses=error_responses(*_SESSION_ERRORS, 404),
)
async def delete_dealer(
    dealer_id: DealerId, current: DealersModule, db: SessionDep, context: AuditContextDep
) -> Response:
    """Borrado lógico; el código queda libre para un registro nuevo."""
    await dealers_service.delete_dealer(db, dealer_id, actor_user_id=current.user.id, context=context)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
