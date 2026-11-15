"""Bitácora del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 5.10).

`record_audit` agrega la fila a la sesión de base de datos de la acción: se confirma en la misma transacción (si la
acción falla, no queda registro). La aplicación nunca actualiza ni borra filas de `audit_logs`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_var
from app.models.audit_log import AuditAction, AuditEntityType, AuditLog
from app.utils.client_ip import client_ip


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
