"""Bitácora de auditoría (BACKOFFICE_DEALERS.spec.md, sección 6.8). Módulo `AUDITORIA`."""

import uuid
from datetime import date
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationInfo, field_validator

from app.models.audit_log import AuditAction, AuditEntityType
from app.schemas.backoffice.common import BackofficeResponse, PageQuery, UserRef

DATE_RANGE_INVALID = "La fecha final no puede ser anterior a la inicial."


class AuditLogQuery(PageQuery):
    """GET /audit-logs. `from` y `to` son fechas AAAA-MM-DD en KIZUNA_TIMEZONE: `from` desde las 00:00 y `to` hasta el
    final del día. Orden fijo: `occurredAt` descendente."""

    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    actor_user_id: uuid.UUID | None = None
    action: AuditAction | None = None
    entity_type: AuditEntityType | None = None
    entity_id: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=100)] = None

    @field_validator("to")
    @classmethod
    def _to_not_before_from(cls, value: date | None, info: ValidationInfo) -> date | None:
        start = info.data.get("from_")
        if value is not None and start is not None and value < start:
            raise ValueError(DATE_RANGE_INVALID)
        return value


class AuditLogItem(BackofficeResponse):
    id: uuid.UUID
    occurred_at: str
    # Nulo en acciones anónimas o de la CLI.
    actor: UserRef | None
    action: str
    entity_type: str | None
    entity_id: str | None
    details: dict[str, Any] | None
    ip_address: str | None
    user_agent: str | None
