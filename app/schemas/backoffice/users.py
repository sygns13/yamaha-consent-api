"""Usuarios del BackOffice y Mi perfil (BACKOFFICE_DEALERS.spec.md, secciones 6.4 y 6.5).

Los tipos de campo (`Dni`, `PersonName`, ...) y `UserCreateRequest` se adelantaron en la fase B2 porque los necesita
la CLI `create-admin` ("las mismas validaciones que la API"). La fase B4 los reutiliza para POST/PUT /users y
PATCH /profile.
"""

import uuid
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from app.core.config import get_settings
from app.models.role import RoleCode
from app.models.user import UserStatus
from app.schemas.backoffice.auth import CodeName
from app.schemas.backoffice.common import (
    BackofficeRequest,
    BackofficeResponse,
    ListQuery,
    SearchText,
    UserRef,
)
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


class UserUpdateRequest(BackofficeRequest):
    """PUT /users/{userId}: reemplazo completo de los datos editables (`username` no se edita: enviarlo → 422).
    `phone` ausente o null lo borra."""

    dni: Dni
    first_names: PersonName
    last_names: PersonName
    email: UserEmail
    phone: Mobile = None
    role_code: RoleCode
    version: int = Field(ge=1)


class ProfileUpdateRequest(BackofficeRequest):
    """PATCH /profile (sección 6.4). Todos opcionales: un campo ausente no cambia. `phone: null` lo borra; nombres o
    correo en null → 422. `dni`, `username` o `roleCode` → 422 (campos no permitidos). `currentPassword` es
    obligatoria (y correcta) solo si cambia el correo."""

    # El valor por defecto (None = ausente) no se valida; un null explícito sí, y no es un texto válido.
    first_names: PersonName = None  # type: ignore[assignment]
    last_names: PersonName = None  # type: ignore[assignment]
    email: UserEmail = None  # type: ignore[assignment]
    phone: Mobile = None
    current_password: str | None = None


UserSortBy = Literal["lastNames", "username", "createdAt", "lastLoginAt"]


class UserListQuery(ListQuery):
    """GET /users. `q` busca en DNI, nombres, apellidos, nombre completo, usuario o correo."""

    q: SearchText = None
    role_code: RoleCode | None = None
    status: UserStatus | None = None
    # lastNames ordena por apellidos y luego nombres.
    sort_by: UserSortBy = "lastNames"


class UserSummary(BackofficeResponse):
    """Ítem de GET /users."""

    id: uuid.UUID
    username: str
    first_names: str
    last_names: str
    dni: str
    email: str
    role: CodeName
    status: UserStatus
    last_login_at: str | None


class UserDetail(UserSummary):
    phone: str | None
    must_change_password: bool
    locked_until: str | None
    created_at: str
    # Nulo si lo creó la CLI.
    created_by: UserRef | None
    updated_at: str
    updated_by: UserRef | None
    version: int


class UserCreatedResponse(BackofficeResponse):
    """POST /users. La contraseña temporal se muestra una sola vez."""

    user: UserDetail
    temporary_password: str


class TemporaryPasswordResponse(BackofficeResponse):
    """POST /users/{userId}/reset-password. Se muestra una sola vez."""

    temporary_password: str
