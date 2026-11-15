"""Seguridad del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, sección 5).

- Chequeo de configuración al arrancar (sección 5.12).
- `BackofficeError` y su handler: `{"detail", "code", "field"?}` (sección 5.8, textos del Anexo A.3).
- Cookie de sesión `__session` (sección 5.3).
- Dependencias: cabecera CSRF (5.6), sesión (5.4), cambio obligatorio de contraseña y módulo del rol.
"""

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import Depends, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import InsecureConfigurationError
from app.db.session import get_session
from app.models.role import ModuleCode
from app.models.user_session import SessionRevokedReason
from app.services.backoffice import sessions as session_service
from app.services.backoffice.sessions import AuthenticatedSession

logger = logging.getLogger(__name__)

BACKOFFICE_PREFIX = "/api/v1/backoffice"
# Nombre fijo: Firebase Hosting elimina todas las cookies de las peticiones que reenvía a Cloud Run salvo __session.
SESSION_COOKIE_NAME = "__session"
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "XMLHttpRequest"
_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def check_backoffice_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app.

    Con APP_ENV=production y BACKOFFICE_SESSION_COOKIE_SECURE=false → InsecureConfigurationError (log CRITICAL): la
    cookie de sesión viajaría también por http, así que la app no arranca y el despliegue en Cloud Run falla.
    """
    if settings.app_env == "production" and not settings.backoffice_session_cookie_secure:
        message = (
            "APP_ENV=production con BACKOFFICE_SESSION_COOKIE_SECURE=false: la cookie de sesión del BackOffice "
            "debe ser Secure en Producción"
        )
        logger.critical(message)
        raise InsecureConfigurationError(message)


# --- Errores (sección 5.8, Anexo A.3) ---


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    USER_INACTIVE = "USER_INACTIVE"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    CSRF_HEADER_MISSING = "CSRF_HEADER_MISSING"
    NOT_FOUND = "NOT_FOUND"
    RESET_LINK_NOT_FOUND = "RESET_LINK_NOT_FOUND"
    DUPLICATE = "DUPLICATE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    LAST_ADMIN = "LAST_ADMIN"
    SELF_ACTION_NOT_ALLOWED = "SELF_ACTION_NOT_ALLOWED"
    RESET_LINK_INVALID = "RESET_LINK_INVALID"
    CURRENT_PASSWORD_INVALID = "CURRENT_PASSWORD_INVALID"
    PASSWORD_POLICY = "PASSWORD_POLICY"
    DISTRICT_NOT_FOUND = "DISTRICT_NOT_FOUND"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"


AUTH_REQUIRED = "Debes iniciar sesión."
SESSION_EXPIRED = "Tu sesión expiró. Inicia sesión nuevamente."
INVALID_CREDENTIALS = "Usuario o contraseña incorrectos."
USER_INACTIVE = "Tu usuario está inactivo. Comunícate con el Administrador."
PASSWORD_CHANGE_REQUIRED = "Debes cambiar tu contraseña antes de continuar."
FORBIDDEN = "No tienes permiso para realizar esta acción."
CSRF_HEADER_MISSING = "Solicitud no permitida."
CURRENT_PASSWORD_INVALID = "La contraseña actual no es correcta."
RESET_LINK_NOT_FOUND = "El enlace no existe."
RESET_LINK_INVALID = "El enlace para restablecer la contraseña no es válido o ya venció. Solicita uno nuevo."
ACCOUNT_LOCKED = (
    "Tu usuario está bloqueado temporalmente por intentos fallidos. Inténtalo nuevamente en {minutes} o "
    "comunícate con el Administrador."
)


class BackofficeError(Exception):
    """Error de negocio del BackOffice. `field` va en camelCase, como en el request. `headers` se agregan a la
    respuesta (p. ej. el Set-Cookie que borra la sesión)."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        detail: str,
        field: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.field = field
        self.extra = extra
        self.headers = headers


async def backoffice_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BackofficeError)
    content: dict[str, Any] = {"detail": exc.detail, "code": exc.code.value}
    if exc.field is not None:
        content["field"] = exc.field
    if exc.extra:
        content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


def invalid_credentials() -> BackofficeError:
    return BackofficeError(401, ErrorCode.INVALID_CREDENTIALS, INVALID_CREDENTIALS)


def minutes_text(minutes: int) -> str:
    """ "1 minuto" en singular, "n minutos" en plural (v1.2: bloqueo, vigencia del enlace de restablecimiento)."""
    return "1 minuto" if minutes == 1 else f"{minutes} minutos"


def account_locked(locked_until: datetime, now: datetime) -> BackofficeError:
    """El mensaje indica los minutos restantes redondeados hacia arriba."""
    minutes = max(1, math.ceil((locked_until - now).total_seconds() / 60))
    return BackofficeError(
        423, ErrorCode.ACCOUNT_LOCKED, ACCOUNT_LOCKED.format(minutes=minutes_text(minutes))
    )


def user_inactive() -> BackofficeError:
    return BackofficeError(403, ErrorCode.USER_INACTIVE, USER_INACTIVE)


# --- Cookie de sesión (sección 5.3) ---


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.backoffice_session_max_hours * 3600,
        path=BACKOFFICE_PREFIX,
        secure=settings.backoffice_session_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=BACKOFFICE_PREFIX,
        secure=settings.backoffice_session_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _session_expired(settings: Settings) -> BackofficeError:
    # La respuesta borra la cookie (sección 5.4, paso 2).
    carrier = Response()
    clear_session_cookie(carrier, settings)
    return BackofficeError(
        401,
        ErrorCode.SESSION_EXPIRED,
        SESSION_EXPIRED,
        headers={"set-cookie": carrier.headers["set-cookie"]},
    )


# Solo documenta la cookie en OpenAPI; la validación real está en las dependencias de sesión.
session_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="BackofficeSession",
    description="Cookie de sesión del BackOffice (HttpOnly; la envía el navegador)",
    auto_error=False,
)


# --- Dependencias ---


async def require_csrf_header(request: Request) -> None:
    """Dependencia del router del BackOffice: POST/PUT/PATCH/DELETE exigen `X-Requested-With: XMLHttpRequest`
    (sección 5.6). Corre antes que la sesión y que la validación del body."""
    if request.method in _CSRF_METHODS and request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise BackofficeError(403, ErrorCode.CSRF_HEADER_MISSING, CSRF_HEADER_MISSING)


async def _authenticate(
    request: Request, db: AsyncSession, settings: Settings, *, allow_pending_password_change: bool
) -> AuthenticatedSession:
    """Sección 5.4. Deja la transacción de `db` cerrada (commit), para que el endpoint abra la suya."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise BackofficeError(401, ErrorCode.AUTH_REQUIRED, AUTH_REQUIRED)

    now = datetime.now(UTC)
    found = await session_service.find_session(db, token)
    if found is None or session_service.is_session_expired(found[0], now=now, settings=settings):
        await db.rollback()
        raise _session_expired(settings)

    user_session, user = found
    if user.is_deleted or not user.is_active:
        reason = (
            SessionRevokedReason.USER_DELETED if user.is_deleted else SessionRevokedReason.USER_DEACTIVATED
        )
        session_service.revoke_session(user_session, reason, now)
        await db.commit()
        logger.info("Sesión del BackOffice revocada: usuario no vigente", extra={"user_id": str(user.id)})
        raise _session_expired(settings)

    if session_service.needs_activity_refresh(user_session, now):
        user_session.last_activity_at = now

    role, modules = await session_service.load_role_and_modules(db, user.role_code)
    await db.commit()

    if user.must_change_password and not allow_pending_password_change:
        raise BackofficeError(403, ErrorCode.PASSWORD_CHANGE_REQUIRED, PASSWORD_CHANGE_REQUIRED)
    return AuthenticatedSession(session=user_session, user=user, role=role, modules=modules)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
_DocumentedCookie = Annotated[str | None, Security(session_cookie_scheme)]


async def get_current_session(
    request: Request, db: SessionDep, settings: SettingsDep, _cookie: _DocumentedCookie = None
) -> AuthenticatedSession:
    """Sesión válida y sin cambio de contraseña pendiente (si no, 403 PASSWORD_CHANGE_REQUIRED)."""
    return await _authenticate(request, db, settings, allow_pending_password_change=False)


async def get_current_session_allowing_password_change(
    request: Request, db: SessionDep, settings: SettingsDep, _cookie: _DocumentedCookie = None
) -> AuthenticatedSession:
    """Solo para GET /auth/me y POST /auth/change-password: aceptan una sesión con cambio de contraseña pendiente."""
    return await _authenticate(request, db, settings, allow_pending_password_change=True)


CurrentSession = Annotated[AuthenticatedSession, Depends(get_current_session)]


def require_module(module: ModuleCode) -> Callable[..., Awaitable[AuthenticatedSession]]:
    """Dependencia: sesión válida cuyo rol tiene `module`; si no, 403 FORBIDDEN. Los permisos se leen de la base en
    cada petición, así que un cambio de rol aplica en la siguiente."""

    async def dependency(current: CurrentSession) -> AuthenticatedSession:
        if module.value not in current.module_codes:
            logger.info(
                "Acceso denegado a un módulo del BackOffice",
                extra={"user_id": str(current.user.id), "module_code": module.value},
            )
            raise BackofficeError(403, ErrorCode.FORBIDDEN, FORBIDDEN)
        return current

    return dependency
