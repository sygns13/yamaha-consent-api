"""Dealers del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.6). Módulo `DEALERS` (Administrador y Operador).

- `dealer_code` es inmutable y único sin distinguir mayúsculas entre los vigentes (índice `uq_dealers_active_code` sobre
  `lower(dealer_code) WHERE is_deleted = 0`); un código eliminado se puede volver a registrar.
- `get_active_dealer_by_code` es el **punto de reutilización** para la siguiente implementación de la landing
  (sección 13, F1).
- Igual que en usuarios (decisiones de la sesión 3): `version` sube solo con PUT; `updated_at` y `updated_by` cambian
  con PUT y DELETE; un PUT sin cambios reales no sube `version` ni deja bitácora.

En los logs solo van el `dealer_id` y el `dealerCode` (sección 5.9): nunca RUC, correo, teléfono ni nombres.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.backoffice_security import (
    DISTRICT_NOT_FOUND,
    BackofficeError,
    ErrorCode,
    not_found,
    version_conflict,
)
from app.models.audit_log import AuditAction, AuditEntityType
from app.models.dealer import Dealer
from app.models.ubigeo import UbigeoDepartment, UbigeoDistrict, UbigeoProvince
from app.schemas.backoffice.auth import CodeName
from app.schemas.backoffice.common import UserRef
from app.schemas.backoffice.dealers import (
    DealerCreateRequest,
    DealerDetail,
    DealerFilterQuery,
    DealerListQuery,
    DealerUpdateRequest,
)
from app.services.backoffice.audit import AuditContext, diff_changes, record_audit
from app.services.backoffice.listing import contains_any, paginate
from app.services.backoffice.refs import user_refs
from app.utils.datetimes import to_kizuna_iso
from app.utils.normalization import normalize_dealer

logger = logging.getLogger(__name__)

DEALER_NOT_FOUND = "No existe el dealer."
DUPLICATE_DEALER_CODE = "Ya existe un dealer con ese código."
_UNIQUE_CODE_INDEX = "uq_dealers_active_code"
_DISTRICT_FK = "fk_dealers_district_ubigeo_ubigeo_districts"

# Campos editables: campo JSON (camelCase, el de `changes` en la bitácora) → atributo.
EDITABLE_FIELDS = {
    "businessName": "business_name",
    "ruc": "ruc",
    "address": "address",
    "districtUbigeo": "district_ubigeo",
    "contactEmail": "contact_email",
    "contactPhone": "contact_phone",
    "contactName": "contact_name",
}

_SORT_COLUMNS = {
    # Sin distinguir mayúsculas, como la unicidad del código.
    "dealerCode": (func.lower(Dealer.dealer_code), Dealer.dealer_code),
    "businessName": (Dealer.business_name,),
    "ruc": (Dealer.ruc,),
    "createdAt": (Dealer.created_at,),
    "updatedAt": (Dealer.updated_at,),
}


@dataclass(frozen=True)
class DealerRecord:
    """Dealer con su distrito, provincia y departamento (lo que muestran el detalle y la exportación)."""

    dealer: Dealer
    district: UbigeoDistrict
    province: UbigeoProvince
    department: UbigeoDepartment

    @property
    def author_ids(self) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        return self.dealer.created_by, self.dealer.updated_by


def _records_statement() -> Select[Any]:
    return (
        select(Dealer, UbigeoDistrict, UbigeoProvince, UbigeoDepartment)
        .join(UbigeoDistrict, UbigeoDistrict.code == Dealer.district_ubigeo)
        .join(UbigeoProvince, UbigeoProvince.code == UbigeoDistrict.province_code)
        .join(UbigeoDepartment, UbigeoDepartment.code == UbigeoProvince.department_code)
        .where(Dealer.is_deleted == 0)
        .execution_options(populate_existing=True)
    )


def dealer_values(dealer: Dealer) -> dict[str, Any]:
    """Campos editables en camelCase, para `changes` (sección 5.10)."""
    return {field: getattr(dealer, attribute) for field, attribute in EDITABLE_FIELDS.items()}


def build_dealer_detail(
    record: DealerRecord, refs: dict[uuid.UUID, UserRef], *, zone: ZoneInfo
) -> DealerDetail:
    dealer = record.dealer
    return DealerDetail(
        id=dealer.id,
        dealer_code=dealer.dealer_code,
        business_name=dealer.business_name,
        ruc=dealer.ruc,
        address=dealer.address,
        district=CodeName(code=record.district.code, name=record.district.name),
        province=CodeName(code=record.province.code, name=record.province.name),
        department=CodeName(code=record.department.code, name=record.department.name),
        contact_email=dealer.contact_email,
        contact_phone=dealer.contact_phone,
        contact_name=dealer.contact_name,
        created_at=to_kizuna_iso(dealer.created_at, zone) or "",
        created_by=refs.get(dealer.created_by) if dealer.created_by else None,
        updated_at=to_kizuna_iso(dealer.updated_at, zone) or "",
        updated_by=refs.get(dealer.updated_by) if dealer.updated_by else None,
        version=dealer.version,
    )


async def _refs_for(db: AsyncSession, records: list[DealerRecord]) -> dict[uuid.UUID, UserRef]:
    return await user_refs(db, (author for record in records for author in record.author_ids))


# --- Lectura ---


async def get_active_dealer_by_code(session: AsyncSession, code: str) -> Dealer | None:
    """Dealer **vigente** con ese código, sin distinguir mayúsculas (trim). Punto de reutilización para la landing
    (sección 13, F1). Usa el índice `uq_dealers_active_code` (`lower(dealer_code) WHERE is_deleted = 0`)."""
    return await session.scalar(
        select(Dealer).where(
            func.lower(Dealer.dealer_code) == func.lower(normalize_dealer(code)), Dealer.is_deleted == 0
        )
    )


async def get_dealer_detail(db: AsyncSession, dealer_id: uuid.UUID, *, zone: ZoneInfo) -> DealerDetail:
    """Dealer vigente; inexistente o eliminado → 404 NOT_FOUND."""
    row = (await db.execute(_records_statement().where(Dealer.id == dealer_id))).first()
    if row is None:
        await _fail(db, not_found(DEALER_NOT_FOUND))
    record = DealerRecord(*row)
    detail = build_dealer_detail(record, await _refs_for(db, [record]), zone=zone)
    # Solo lectura. Se cierra con commit: un rollback expiraría los objetos de la sesión.
    await db.commit()
    return detail


def _filtered_statement(query: DealerFilterQuery) -> Select[Any]:
    statement = _records_statement()
    if query.q:
        statement = statement.where(
            contains_any(query.q, Dealer.dealer_code, Dealer.business_name, Dealer.ruc, Dealer.contact_name)
        )
    if query.department_code:
        statement = statement.where(UbigeoProvince.department_code == query.department_code)
    if query.province_code:
        statement = statement.where(UbigeoDistrict.province_code == query.province_code)
    if query.district_ubigeo:
        statement = statement.where(Dealer.district_ubigeo == query.district_ubigeo)
    descending = query.sort_order == "desc"
    order = [column.desc() if descending else column.asc() for column in _SORT_COLUMNS[query.sort_by]]
    if query.sort_by == "businessName":
        # La razón social es opcional: los dealers sin ella van al final en ambos sentidos.
        order = [clause.nulls_last() for clause in order]
    # Desempate por id: la paginación es estable aunque haya valores repetidos.
    return statement.order_by(*order, Dealer.id.asc())


async def list_dealers(
    db: AsyncSession, query: DealerListQuery, *, zone: ZoneInfo
) -> tuple[list[DealerDetail], int]:
    """GET /dealers: vigentes, con búsqueda, filtros de ubigeo, orden y paginación."""
    rows, total = await paginate(db, _filtered_statement(query), page=query.page, page_size=query.page_size)
    records = [DealerRecord(*row) for row in rows]
    refs = await _refs_for(db, records)
    items = [build_dealer_detail(record, refs, zone=zone) for record in records]
    await db.commit()
    return items, total


async def all_dealers(
    db: AsyncSession, query: DealerFilterQuery
) -> tuple[list[DealerRecord], dict[uuid.UUID, UserRef]]:
    """GET /dealers/export: mismos filtros y orden que la lista, sin paginación. No cierra la transacción (la
    exportación registra su bitácora después)."""
    rows = (await db.execute(_filtered_statement(query))).all()
    records = [DealerRecord(*row) for row in rows]
    return records, await _refs_for(db, records)


# --- Escritura ---


async def _fail(db: AsyncSession, error: BackofficeError) -> NoReturn:
    await db.rollback()
    raise error


def _district_not_found() -> BackofficeError:
    return BackofficeError(422, ErrorCode.DISTRICT_NOT_FOUND, DISTRICT_NOT_FOUND, field="districtUbigeo")


def _duplicate_code() -> BackofficeError:
    return BackofficeError(409, ErrorCode.DUPLICATE, DUPLICATE_DEALER_CODE, field="dealerCode")


async def _ensure_district_exists(db: AsyncSession, code: str) -> None:
    if await db.scalar(select(UbigeoDistrict.code).where(UbigeoDistrict.code == code)) is None:
        await _fail(db, _district_not_found())


async def _flush(db: AsyncSession) -> None:
    """`flush`; la carrera de dos altas del mismo código (índice único) → 409 DUPLICATE; distrito inexistente (FK) →
    422 DISTRICT_NOT_FOUND."""
    try:
        await db.flush()
    except IntegrityError as exc:
        message = str(exc.orig)
        await db.rollback()
        if _UNIQUE_CODE_INDEX in message:
            raise _duplicate_code() from None
        if _DISTRICT_FK in message:
            raise _district_not_found() from None
        raise


def _record(
    db: AsyncSession,
    action: AuditAction,
    dealer: Dealer,
    *,
    actor_user_id: uuid.UUID,
    context: AuditContext,
    details: dict[str, Any],
) -> None:
    record_audit(
        db,
        action,
        context=context,
        actor_user_id=actor_user_id,
        entity_type=AuditEntityType.DEALER,
        entity_id=dealer.id,
        details=details,
    )


async def create_dealer(
    db: AsyncSession, data: DealerCreateRequest, *, actor_user_id: uuid.UUID, context: AuditContext
) -> uuid.UUID:
    """POST /dealers. 422 DISTRICT_NOT_FOUND; 409 DUPLICATE (`dealerCode`, sin distinguir mayúsculas). Hace commit y
    devuelve el id."""
    await _ensure_district_exists(db, data.district_ubigeo)
    if await get_active_dealer_by_code(db, data.dealer_code) is not None:
        await _fail(db, _duplicate_code())

    dealer = Dealer(
        dealer_code=data.dealer_code,
        business_name=data.business_name,
        ruc=data.ruc,
        address=data.address,
        district_ubigeo=data.district_ubigeo,
        contact_email=data.contact_email,
        contact_phone=data.contact_phone,
        contact_name=data.contact_name,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    db.add(dealer)
    await _flush(db)
    _record(
        db,
        AuditAction.DEALER_CREATED,
        dealer,
        actor_user_id=actor_user_id,
        context=context,
        details={"dealerCode": dealer.dealer_code, **dealer_values(dealer)},
    )
    await db.commit()
    logger.info("Dealer registrado", extra={"dealer_id": str(dealer.id), "dealer_code": dealer.dealer_code})
    return dealer.id


async def _locked_dealer(db: AsyncSession, dealer_id: uuid.UUID) -> Dealer:
    dealer = await db.scalar(
        select(Dealer)
        .where(Dealer.id == dealer_id, Dealer.is_deleted == 0)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dealer is None:
        await _fail(db, not_found(DEALER_NOT_FOUND))
    return dealer


async def update_dealer(
    db: AsyncSession,
    dealer_id: uuid.UUID,
    data: DealerUpdateRequest,
    *,
    actor_user_id: uuid.UUID,
    context: AuditContext,
) -> None:
    """PUT /dealers/{dealerId}: reemplazo completo (los opcionales en null se borran). Chequeos: 404 →
    VERSION_CONFLICT → DISTRICT_NOT_FOUND. Hace commit."""
    dealer = await _locked_dealer(db, dealer_id)
    if dealer.version != data.version:
        await _fail(db, version_conflict())
    changes = diff_changes(
        dealer_values(dealer),
        {field: getattr(data, attribute) for field, attribute in EDITABLE_FIELDS.items()},
    )
    if "districtUbigeo" in changes:
        await _ensure_district_exists(db, data.district_ubigeo)
    if not changes:
        await db.rollback()
        return

    for field, change in changes.items():
        setattr(dealer, EDITABLE_FIELDS[field], change["to"])
    dealer.version += 1
    dealer.updated_at = datetime.now(UTC)
    dealer.updated_by = actor_user_id
    await _flush(db)
    _record(
        db,
        AuditAction.DEALER_UPDATED,
        dealer,
        actor_user_id=actor_user_id,
        context=context,
        details={"dealerCode": dealer.dealer_code, "changes": changes},
    )
    await db.commit()
    logger.info("Dealer actualizado", extra={"dealer_id": str(dealer.id), "dealer_code": dealer.dealer_code})


async def delete_dealer(
    db: AsyncSession, dealer_id: uuid.UUID, *, actor_user_id: uuid.UUID, context: AuditContext
) -> None:
    """Borrado lógico: el código queda libre para un registro nuevo. Hace commit."""
    now = datetime.now(UTC)
    dealer = await _locked_dealer(db, dealer_id)
    dealer.is_deleted = 1
    dealer.deleted_at = now
    dealer.deleted_by = actor_user_id
    dealer.updated_at = now
    dealer.updated_by = actor_user_id
    _record(
        db,
        AuditAction.DEALER_DELETED,
        dealer,
        actor_user_id=actor_user_id,
        context=context,
        details={"dealerCode": dealer.dealer_code},
    )
    await db.commit()
    logger.info("Dealer eliminado", extra={"dealer_id": str(dealer.id), "dealer_code": dealer.dealer_code})
