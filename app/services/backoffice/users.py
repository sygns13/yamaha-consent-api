"""Usuarios del BackOffice (BACKOFFICE_DEALERS.spec.md, secciones 5.2, 5.11 y 6.5).

Se adelanta en la fase B2 lo que necesita la CLI (`create-admin`, `reset-password`): alta con validación y unicidad,
y contraseña temporal. La fase B4 lo reutiliza para POST /users y POST /users/{id}/reset-password.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwords
from app.core.backoffice_security import BackofficeError, ErrorCode
from app.core.config import Settings
from app.models.audit_log import AuditAction, AuditEntityType
from app.models.user import User
from app.models.user_session import SessionRevokedReason
from app.schemas.backoffice.users import UserCreateRequest
from app.services.backoffice.audit import AuditContext, record_audit
from app.services.backoffice.password_reset import invalidate_pending_reset_tokens
from app.services.backoffice.sessions import revoke_user_sessions

logger = logging.getLogger(__name__)

# details.source de USER_CREATED y USER_PASSWORD_RESET_BY_ADMIN (sección 5.10).
SOURCE_API = "API"
SOURCE_CLI = "CLI"

# Unicidad entre usuarios vigentes (Anexo A.3): campo → (índice parcial, mensaje). En el orden en que se valida.
_UNIQUE_FIELDS = {
    "dni": ("uq_users_active_dni", "Ya existe un usuario con ese DNI."),
    "username": ("uq_users_active_username", "Ya existe un usuario con ese nombre de usuario."),
    "email": ("uq_users_active_email", "Ya existe un usuario con ese correo."),
}


def _duplicate(field: str) -> BackofficeError:
    return BackofficeError(409, ErrorCode.DUPLICATE, _UNIQUE_FIELDS[field][1], field=field)


async def ensure_unique_user_fields(
    db: AsyncSession, *, exclude_user_id: uuid.UUID | None = None, **values: str
) -> None:
    """409 DUPLICATE (con su `field`) si otro usuario vigente ya usa alguno de los valores (dni, username, email)."""
    for field in _UNIQUE_FIELDS:
        if field not in values:
            continue
        statement = (
            select(User.id).where(getattr(User, field) == values[field], User.is_deleted == 0).limit(1)
        )
        if exclude_user_id is not None:
            statement = statement.where(User.id != exclude_user_id)
        if await db.scalar(statement) is not None:
            raise _duplicate(field)


def duplicate_from_integrity_error(exc: IntegrityError) -> BackofficeError | None:
    """Traduce la violación de un índice único parcial (carrera entre dos altas) al mismo 409 DUPLICATE."""
    message = str(exc.orig)
    for field, (index_name, _) in _UNIQUE_FIELDS.items():
        if index_name in message:
            return _duplicate(field)
    return None


async def create_user(
    db: AsyncSession,
    data: UserCreateRequest,
    *,
    settings: Settings,
    context: AuditContext,
    actor_user_id: uuid.UUID | None = None,
    source: str | None = None,
) -> tuple[User, str]:
    """Alta con contraseña temporal y `must_change_password = true`. Devuelve (usuario, contraseña temporal), que se
    muestra una sola vez. No hace commit: lo hace quien llama."""
    await ensure_unique_user_fields(db, dni=data.dni, username=data.username, email=data.email)
    temporary_password = passwords.generate_temporary_password(
        min_length=settings.backoffice_password_min_length, username=data.username, dni=data.dni
    )
    user = User(
        role_code=data.role_code.value,
        dni=data.dni,
        first_names=data.first_names,
        last_names=data.last_names,
        username=data.username,
        email=data.email,
        phone=data.phone,
        password_hash=passwords.hash_password(temporary_password),
        must_change_password=True,
        is_active=True,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        duplicate = duplicate_from_integrity_error(exc)
        if duplicate is None:
            raise
        raise duplicate from None

    details: dict[str, str | None] = {
        "dni": user.dni,
        "firstNames": user.first_names,
        "lastNames": user.last_names,
        "username": user.username,
        "email": user.email,
        "phone": user.phone,
        "roleCode": user.role_code,
    }
    if source is not None:
        details["source"] = source
    record_audit(
        db,
        AuditAction.USER_CREATED,
        context=context,
        actor_user_id=actor_user_id,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        details=details,
    )
    logger.info("Usuario del BackOffice creado", extra={"user_id": str(user.id), "source": source})
    return user, temporary_password


async def reset_temporary_password(
    db: AsyncSession,
    user: User,
    *,
    settings: Settings,
    context: AuditContext,
    actor_user_id: uuid.UUID | None,
    source: str,
) -> str:
    """Contraseña temporal generada por un Administrador o la CLI: `must_change_password = true`, desbloquea, revoca
    todas las sesiones (ADMIN_PASSWORD_RESET) e invalida los enlaces de restablecimiento sin usar. No hace commit."""
    now = datetime.now(UTC)
    temporary_password = passwords.generate_temporary_password(
        min_length=settings.backoffice_password_min_length, username=user.username, dni=user.dni
    )
    user.password_hash = passwords.hash_password(temporary_password)
    user.must_change_password = True
    user.failed_login_attempts = 0
    user.locked_until = None
    await revoke_user_sessions(db, user.id, SessionRevokedReason.ADMIN_PASSWORD_RESET, now)
    await invalidate_pending_reset_tokens(db, user.id, now)
    record_audit(
        db,
        AuditAction.USER_PASSWORD_RESET_BY_ADMIN,
        context=context,
        actor_user_id=actor_user_id,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        details={"source": source},
    )
    logger.info("Contraseña temporal generada", extra={"user_id": str(user.id), "source": source})
    return temporary_password
