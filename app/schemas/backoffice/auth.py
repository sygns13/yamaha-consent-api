"""Schemas de autenticación del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.3)."""

import uuid
from typing import Annotated

from pydantic import StringConstraints

from app.core.passwords import PASSWORD_MAX_LENGTH
from app.schemas.backoffice.common import BackofficeRequest, BackofficeResponse

USERNAME_MAX_LENGTH = 30


class LoginRequest(BackofficeRequest):
    username: Annotated[str, StringConstraints(min_length=1, max_length=USERNAME_MAX_LENGTH)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class ChangePasswordRequest(BackofficeRequest):
    current_password: Annotated[str, StringConstraints(min_length=1)]
    # Sin restricciones de Pydantic: el largo (incluido el vacío) se informa como 422 PASSWORD_POLICY.
    new_password: str


class CodeName(BackofficeResponse):
    code: str
    name: str


class Me(BackofficeResponse):
    """Usuario con sesión: respuesta de login, GET /auth/me y (B4) GET/PATCH /profile."""

    id: uuid.UUID
    username: str
    first_names: str
    last_names: str
    dni: str
    email: str
    phone: str | None
    role: CodeName
    # Ordenados por sort_order.
    modules: list[CodeName]
    must_change_password: bool
    last_login_at: str | None
    session_expires_at: str


class PasswordPolicyResponse(BackofficeResponse):
    min_length: int
    max_length: int
    require_uppercase: bool
    require_lowercase: bool
    require_digit: bool
    require_special: bool


# --- Recuperación de contraseña (fase B3) ---


class ForgotPasswordRequest(BackofficeRequest):
    # Usuario o correo; sin distinguir mayúsculas.
    identifier: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=254)]


class PasswordResetRequest(BackofficeRequest):
    token: Annotated[str, StringConstraints(min_length=1)]
    # Sin restricciones de Pydantic: el largo (incluido el vacío) se informa como 422 PASSWORD_POLICY.
    new_password: str


class DetailResponse(BackofficeResponse):
    detail: str


class ResetTokenStatusResponse(BackofficeResponse):
    # VALID | EXPIRED | USED | INVALID
    token_status: str
