"""Campos de un usuario del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.5).

Se adelantan en la fase B2 porque los necesita la CLI `create-admin` ("las mismas validaciones que la API"). La fase
B4 los reutiliza para POST/PUT /users y PATCH /profile.
"""

from typing import Annotated

from pydantic import AfterValidator

from app.core.config import get_settings
from app.models.role import RoleCode
from app.schemas.backoffice.common import BackofficeRequest
from app.utils.normalization import (
    normalize_email,
    normalize_person_name,
    normalize_phone,
    normalize_username,
)
from app.utils.validation import (
    validate_dni,
    validate_mobile,
    validate_person_name,
    validate_user_email,
    validate_username,
)


def _clean_dni(value: str) -> str:
    return validate_dni(value.strip())


def _clean_person_name(value: str) -> str:
    return validate_person_name(normalize_person_name(value))


def _clean_username(value: str) -> str:
    return validate_username(normalize_username(value))


def _clean_email(value: str) -> str:
    return validate_user_email(normalize_email(value) or "")


def _clean_phone(value: str | None) -> str | None:
    # Opcional: null o vacío = sin celular.
    if value is None or not value.strip():
        return None
    return validate_mobile(normalize_phone(value, get_settings().phone_country_prefix))


Dni = Annotated[str, AfterValidator(_clean_dni)]
PersonName = Annotated[str, AfterValidator(_clean_person_name)]
Username = Annotated[str, AfterValidator(_clean_username)]
UserEmail = Annotated[str, AfterValidator(_clean_email)]
Mobile = Annotated[str | None, AfterValidator(_clean_phone)]


class UserCreateRequest(BackofficeRequest):
    """POST /users (B4) y CLI `create-admin` (rol ADMIN)."""

    dni: Dni
    first_names: PersonName
    last_names: PersonName
    username: Username
    email: UserEmail
    phone: Mobile = None
    role_code: RoleCode
