"""Bitácora de auditoría (BACKOFFICE_DEALERS.spec.md, sección 6.8). Módulo `AUDITORIA` (solo Administrador)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.backoffice_security import BACKOFFICE_PREFIX, SessionDep, SettingsDep, require_module
from app.models.role import ModuleCode
from app.schemas.backoffice.audit import AuditLogItem, AuditLogQuery
from app.schemas.backoffice.common import Page, error_responses
from app.services.backoffice import audit as audit_service
from app.services.backoffice.sessions import AuthenticatedSession

router = APIRouter(prefix=BACKOFFICE_PREFIX, tags=["backoffice-audit"])

AuditModule = Annotated[AuthenticatedSession, Depends(require_module(ModuleCode.AUDITORIA))]


@router.get(
    "/audit-logs",
    response_model=Page[AuditLogItem],
    summary="Bitácora de auditoría",
    responses=error_responses(401, 403),
)
async def list_audit_logs(
    query: Annotated[AuditLogQuery, Query()], _current: AuditModule, db: SessionDep, settings: SettingsDep
) -> Page[AuditLogItem]:
    """Filtros: `from` y `to` (AAAA-MM-DD en hora de Lima; `to` incluye todo su día; `from > to` → 422),
    `actorUserId`, `action`, `entityType`, `entityId`. Orden fijo: `occurredAt` descendente."""
    items, total = await audit_service.list_audit_logs(db, query, zone=settings.kizuna_zone)
    return Page[AuditLogItem](items=items, total=total, page=query.page, page_size=query.page_size)
