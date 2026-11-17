"""Bitácora del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 5.10).

`record_audit` agrega la fila a la sesión de base de datos de la acción: se confirma en la misma transacción (si la
acción falla, no queda registro). La aplicación nunca actualiza ni borra filas de `audit_logs`.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_var
from app.models.audit_log import AuditAction, AuditEntityType, AuditLog
from app.schemas.backoffice.audit import AuditLogItem, AuditLogQuery
from app.services.backoffice.listing import paginate
from app.services.backoffice.refs import user_refs
from app.utils.client_ip import client_ip
from app.utils.datetimes import to_kizuna_iso


@dataclass(frozen=True)
class AuditContext:
    """Datos de la petición que se guardan con cada acción. La CLI usa `AuditContext()` (todo nulo)."""

    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def from_request(cls, request: Request) -> "AuditContext":
        return cls(
            ip_address=client_ip(
                request.headers.get("x-forwarded-for"), request.client.host if request.client else None
            ),
            user_agent=request.headers.get("user-agent"),
            request_id=request_id_var.get(),
        )


def get_audit_context(request: Request) -> AuditContext:
    """Dependencia de FastAPI."""
    return AuditContext.from_request(request)


AuditContextDep = Annotated[AuditContext, Depends(get_audit_context)]


def diff_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """`{"campo": {"from": x, "to": y}}` solo con los campos de `after` cuyo valor cambió (sección 5.10). Las claves
    van en camelCase, como en el request."""
    return {
        field: {"from": before.get(field), "to": value}
        for field, value in after.items()
        if before.get(field) != value
    }


def record_audit(
    session: AsyncSession,
    action: AuditAction,
    *,
    context: AuditContext,
    actor_user_id: uuid.UUID | None = None,
    entity_type: AuditEntityType | None = None,
    entity_id: uuid.UUID | str | None = None,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> AuditLog:
    """`details` nunca lleva contraseñas, hashes ni tokens."""
    entry = AuditLog(
        occurred_at=occurred_at or datetime.now(UTC),
        actor_user_id=actor_user_id,
        action=action.value,
        entity_type=entity_type.value if entity_type else None,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=details,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        request_id=context.request_id,
    )
    session.add(entry)
    return entry


# --- Consulta (GET /audit-logs, sección 6.8) ---


async def list_audit_logs(
    db: AsyncSession, query: AuditLogQuery, *, zone: ZoneInfo
) -> tuple[list[AuditLogItem], int]:
    """Filtros por fecha (`from` desde las 00:00 y `to` hasta el final del día, en `zone`), actor, acción, tipo y id de
    entidad. Orden fijo: `occurredAt` descendente (desempate por id)."""
    statement = select(AuditLog)
    if query.from_ is not None:
        statement = statement.where(
            AuditLog.occurred_at >= datetime.combine(query.from_, time.min, tzinfo=zone)
        )
    if query.to is not None:
        next_day = datetime.combine(query.to + timedelta(days=1), time.min, tzinfo=zone)
        statement = statement.where(AuditLog.occurred_at < next_day)
    if query.actor_user_id is not None:
        statement = statement.where(AuditLog.actor_user_id == query.actor_user_id)
    if query.action is not None:
        statement = statement.where(AuditLog.action == query.action.value)
    if query.entity_type is not None:
        statement = statement.where(AuditLog.entity_type == query.entity_type.value)
    if query.entity_id:
        statement = statement.where(AuditLog.entity_id == query.entity_id)
    statement = statement.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())

    rows, total = await paginate(db, statement, page=query.page, page_size=query.page_size)
    entries: list[AuditLog] = [row[0] for row in rows]
    refs = await user_refs(db, (entry.actor_user_id for entry in entries))
    items = [
        AuditLogItem(
            id=entry.id,
            occurred_at=to_kizuna_iso(entry.occurred_at, zone) or "",
            actor=refs.get(entry.actor_user_id) if entry.actor_user_id else None,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            details=entry.details,
            # INET llega como ipaddress.IPv4Address/IPv6Address.
            ip_address=str(entry.ip_address) if entry.ip_address is not None else None,
            user_agent=entry.user_agent,
        )
        for entry in entries
    ]
    await db.commit()
    return items, total
