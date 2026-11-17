"""Dealers del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.6).

Campos obligatorios: `dealerCode` (solo al crear), `ruc`, `address`, `districtUbigeo`, `contactEmail`. Opcionales (vacío
→ null): `businessName`, `contactPhone`, `contactName`.
"""

import uuid
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from app.schemas.backoffice.auth import CodeName
from app.schemas.backoffice.common import (
    BackofficeRequest,
    BackofficeResponse,
    PageQuery,
    QueryModel,
    SearchText,
    SortOrder,
    UserRef,
)
from app.utils.normalization import (
    normalize_contact_phone,
    normalize_dealer,
    normalize_email,
    normalize_person_name,
    normalize_text,
)
from app.utils.ruc import normalize_ruc, validate_ruc
from app.utils.validation import (
    DEALER_CODE_EMPTY,
    PERSON_NAME_EMPTY,
    validate_contact_phone,
    validate_dealer,
    validate_district_ubigeo,
    validate_person_name,
    validate_user_email,
)

BUSINESS_NAME_MAX_LENGTH = 250
ADDRESS_MAX_LENGTH = 250
CONTACT_NAME_MAX_LENGTH = 150


def _too_long(max_length: int) -> str:
    return f"Debe tener como máximo {max_length} caracteres"


def _clean_dealer_code(value: str) -> str:
    # Mismo largo y normalización que consents.dealer_code (solo trim, validate_dealer), con el texto del Anexo A.3
    # para el vacío.
    code = normalize_dealer(value)
    if not code:
        raise ValueError(DEALER_CODE_EMPTY)
    return validate_dealer(code)


def _clean_business_name(value: str | None) -> str | None:
    if value is None:
        return None
    name = normalize_text(value)
    if len(name) > BUSINESS_NAME_MAX_LENGTH:
        raise ValueError(_too_long(BUSINESS_NAME_MAX_LENGTH))
    return name or None


def _clean_ruc(value: str) -> str:
    return validate_ruc(normalize_ruc(value))


def _clean_address(value: str) -> str:
    address = normalize_text(value)
    if not address:
        raise ValueError(PERSON_NAME_EMPTY)
    if len(address) > ADDRESS_MAX_LENGTH:
        raise ValueError(_too_long(ADDRESS_MAX_LENGTH))
    return address


def _clean_district(value: str) -> str:
    return validate_district_ubigeo(value.strip())


def _clean_contact_email(value: str) -> str:
    return validate_user_email(normalize_email(value) or "")


def _clean_contact_phone(value: str | None) -> str | None:
    if value is None:
        return None
    phone = normalize_contact_phone(value)
    return validate_contact_phone(phone) if phone else None


def _clean_contact_name(value: str | None) -> str | None:
    if value is None:
        return None
    name = normalize_person_name(value)
    return validate_person_name(name, max_length=CONTACT_NAME_MAX_LENGTH) if name else None


DealerCode = Annotated[str, AfterValidator(_clean_dealer_code)]
BusinessName = Annotated[str | None, AfterValidator(_clean_business_name)]
Ruc = Annotated[str, AfterValidator(_clean_ruc)]
Address = Annotated[str, AfterValidator(_clean_address)]
DistrictUbigeo = Annotated[str, AfterValidator(_clean_district)]
ContactEmail = Annotated[str, AfterValidator(_clean_contact_email)]
ContactPhone = Annotated[str | None, AfterValidator(_clean_contact_phone)]
ContactName = Annotated[str | None, AfterValidator(_clean_contact_name)]


class DealerFields(BackofficeRequest):
    """Campos editables. Un opcional ausente o null queda en null (en PUT, se borra)."""

    business_name: BusinessName = None
    ruc: Ruc
    address: Address
    district_ubigeo: DistrictUbigeo
    contact_email: ContactEmail
    contact_phone: ContactPhone = None
    contact_name: ContactName = None


class DealerCreateRequest(DealerFields):
    """POST /dealers."""

    dealer_code: DealerCode


class DealerUpdateRequest(DealerFields):
    """PUT /dealers/{dealerId}: reemplazo completo de los campos editables. `dealerCode` no se edita (enviarlo →
    422)."""

    version: int = Field(ge=1)


def _ubigeo_code(length: int) -> AfterValidator:
    """Filtro de ubigeo: vacío = sin filtro; si no, `length` dígitos."""

    def check(value: str | None) -> str | None:
        if not value:
            return None
        if len(value) != length or not value.isascii() or not value.isdigit():
            raise ValueError(f"Debe tener {length} dígitos")
        return value

    return AfterValidator(check)


DealerSortBy = Literal["dealerCode", "businessName", "ruc", "createdAt", "updatedAt"]


class DealerFilterQuery(QueryModel):
    """Filtros y orden de GET /dealers y GET /dealers/export. `q`: código, razón social, RUC o persona de contacto."""

    q: SearchText = None
    department_code: Annotated[str | None, _ubigeo_code(2)] = None
    province_code: Annotated[str | None, _ubigeo_code(4)] = None
    district_ubigeo: Annotated[str | None, _ubigeo_code(6)] = None
    sort_by: DealerSortBy = "dealerCode"
    sort_order: SortOrder = "asc"


class DealerListQuery(DealerFilterQuery, PageQuery):
    """GET /dealers: filtros, orden y paginación."""


class DealerLookupQuery(QueryModel):
    """GET /dealers/lookup?dealerCode=: trim; sin distinguir mayúsculas."""

    dealer_code: DealerCode


class DealerDetail(BackofficeResponse):
    id: uuid.UUID
    dealer_code: str
    business_name: str | None
    ruc: str
    address: str
    district: CodeName
    province: CodeName
    department: CodeName
    contact_email: str
    contact_phone: str | None
    contact_name: str | None
    created_at: str
    # Nulo si no hay autor (CLI o usuario sin registrar).
    created_by: UserRef | None
    updated_at: str
    updated_by: UserRef | None
    version: int
