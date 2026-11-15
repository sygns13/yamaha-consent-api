"""Inicio y cierre de sesión y cambio de contraseña del BackOffice (BACKOFFICE_DEALERS.spec.md, secciones 5.5 y
6.3).

En los logs solo va el `id` del usuario: nunca el usuario escrito, la contraseña ni el token.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwords
from app.core.backoffice_security import (
    CURRENT_PASSWORD_INVALID,
    BackofficeError,
    ErrorCode,
    account_locked,
    invalid_credentials,
    user_inactive,
)
from app.core.config import Settings
from app.models.audit_log import AuditAction, AuditEntityType
from app.models.user import User
from app.models.user_session import SessionRevokedReason
from app.schemas.backoffice.auth import CodeName, Me
from app.services.backoffice import sessions as session_service
from app.services.backoffice.audit import AuditContext, record_audit
from app.services.backoffice.password_reset import invalidate_pending_reset_tokens
from app.services.backoffice.sessions import AuthenticatedSession
from app.utils.datetimes import to_kizuna_iso
from app.utils.normalization import normalize_username

logger = logging.getLogger(__name__)

# details.reason de AUTH_LOGIN_FAILED (sección 5.10).
REASON_UNKNOWN_USER = "UNKNOWN_USER"
REASON_BAD_PASSWORD = "BAD_PASSWORD"
REASON_LOCKED = "LOCKED"
REASON_INACTIVE = "INACTIVE"


def build_me(current: AuthenticatedSession, zone: ZoneInfo) -> Me:
    user = current.user
    return Me(
        id=user.id,
        username=user.username,
        first_names=user.first_names,
        last_names=user.last_names,
        dni=user.dni,
        email=user.email,
        phone=user.phone,
        role=CodeName(code=current.role.code, name=current.role.name),
        modules=[CodeName(code=module.code, name=module.name) for module in current.modules],
        must_change_password=user.must_change_password,
        last_login_at=to_kizuna_iso(user.last_login_at, zone),
        session_expires_at=to_kizuna_iso(current.session.expires_at, zone) or "",
    )


def _record_login_failure(db: AsyncSession, context: AuditContext, user: User | None, reason: str) -> None:
    # Sin el usuario escrito: un usuario inexistente queda con actor y entity_id nulos (sección 4.3: el actor es nulo
    # en el "login fallido de usuario inexistente"); el de un usuario existente queda con su id.
    record_audit(
        db,
        AuditAction.AUTH_LOGIN_FAILED,
        context=context,
        actor_user_id=user.id if user else None,
        entity_type=AuditEntityType.USER,
        entity_id=user.id if user else None,
        details={"reason": reason},
    )
    logger.warning(
        "Inicio de sesión fallido en el BackOffice",
        extra={"user_id": str(user.id) if user else None, "reason": reason},
    )


async def login(
    db: AsyncSession, *, username: str, password: str, settings: Settings, context: AuditContext
) -> tuple[str, AuthenticatedSession]:
    """Sección 5.5. Devuelve (token de la cookie, sesión). Los intentos fallidos se confirman con su propio commit
    antes de lanzar el error."""
    now = datetime.now(UTC)
    user = await db.scalar(
        select(User)
        .where(User.username == normalize_username(username), User.is_deleted == 0)
        .with_for_update()
    )

    if user is None:
        passwords.verify_dummy_password(password)
        _record_login_failure(db, context, None, REASON_UNKNOWN_USER)
        await db.commit()
        raise invalid_credentials()

    if user.locked_until is not None and user.locked_until > now:
        # No se verifica la contraseña ni se suma intento.
        _record_login_failure(db, context, user, REASON_LOCKED)
        await db.commit()
        raise account_locked(user.locked_until, now)

    if not passwords.verify_password(user.password_hash, password):
        user.failed_login_attempts += 1
        _record_login_failure(db, context, user, REASON_BAD_PASSWORD)
        if user.failed_login_attempts >= settings.backoffice_login_max_failed_attempts:
            locked_until = now + timedelta(minutes=settings.backoffice_login_lock_minutes)
            user.locked_until = locked_until
            user.failed_login_attempts = 0
            record_audit(
                db,
                AuditAction.AUTH_ACCOUNT_LOCKED,
                context=context,
                actor_user_id=user.id,
                entity_type=AuditEntityType.USER,
                entity_id=user.id,
                details={"lockedUntil": to_kizuna_iso(locked_until, settings.kizuna_zone)},
            )
            await db.commit()
            logger.warning(
                "Usuario del BackOffice bloqueado por intentos fallidos", extra={"user_id": str(user.id)}
            )
            raise account_locked(locked_until, now)
        await db.commit()
        raise invalid_credentials()

    if not user.is_active:
        _record_login_failure(db, context, user, REASON_INACTIVE)
        await db.commit()
        raise user_inactive()

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    await session_service.purge_user_sessions(db, user.id, now)
    token, user_session = session_service.create_session(
        db, user.id, settings=settings, now=now, context=context
    )
    record_audit(
        db,
        AuditAction.AUTH_LOGIN_SUCCEEDED,
        context=context,
        actor_user_id=user.id,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
    )
    role, modules = await session_service.load_role_and_modules(db, user.role_code)
    await db.commit()
    logger.info("Inicio de sesión en el BackOffice", extra={"user_id": str(user.id)})
    return token, AuthenticatedSession(session=user_session, user=user, role=role, modules=modules)


async def logout(db: AsyncSession, token: str | None, *, settings: Settings, context: AuditContext) -> None:
    """Revoca la sesión del token si sigue vigente y registra AUTH_LOGOUT. Sin sesión vigente no hace nada
    (idempotente)."""
    if not token:
        return
    now = datetime.now(UTC)
    found = await session_service.find_session(db, token)
    if found is None or session_service.is_session_expired(found[0], now=now, settings=settings):
        await db.rollback()
        return
    user_session, user = found
    session_service.revoke_session(user_session, SessionRevokedReason.LOGOUT, now)
    record_audit(
        db,
        AuditAction.AUTH_LOGOUT,
        context=context,
        actor_user_id=user.id,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
    )
    await db.commit()
    logger.info("Cierre de sesión en el BackOffice", extra={"user_id": str(user.id)})


async def change_password(
    db: AsyncSession,
    current: AuthenticatedSession,
    *,
    current_password: str,
    new_password: str,
    settings: Settings,
    context: AuditContext,
) -> tuple[str, AuthenticatedSession]:
    """Sección 6.3, pasos 1–5. Revoca todas las sesiones del usuario (las demás y la actual) y crea una nueva: la
    rotación de la actual. También invalida los enlaces de restablecimiento sin usar (sección 4.3). Devuelve (token
    de la nueva cookie, nueva sesión)."""
    now = datetime.now(UTC)
    user = await db.get(User, current.user.id, with_for_update=True, populate_existing=True)
    assert user is not None  # la sesión acaba de validarse

    if not passwords.verify_password(user.password_hash, current_password):
        await db.rollback()
        raise BackofficeError(
            422, ErrorCode.CURRENT_PASSWORD_INVALID, CURRENT_PASSWORD_INVALID, field="currentPassword"
        )
    violations = passwords.policy_violations(
        new_password,
        min_length=settings.backoffice_password_min_length,
        username=user.username,
        dni=user.dni,
        current_hash=user.password_hash,
    )
    if violations:
        await db.rollback()
        raise BackofficeError(
            422, ErrorCode.PASSWORD_POLICY, passwords.policy_error_detail(violations), field="newPassword"
        )

    forced = user.must_change_password
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = now
    await session_service.revoke_user_sessions(db, user.id, SessionRevokedReason.PASSWORD_CHANGED, now)
    await invalidate_pending_reset_tokens(db, user.id, now)
    token, user_session = session_service.create_session(
        db, user.id, settings=settings, now=now, context=context
    )
    record_audit(
        db,
        AuditAction.AUTH_PASSWORD_CHANGED,
        context=context,
        actor_user_id=user.id,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        details={"forced": forced},
    )
    await db.commit()
    logger.info("Contraseña del BackOffice cambiada", extra={"user_id": str(user.id)})
    return token, AuthenticatedSession(
        session=user_session, user=user, role=current.role, modules=current.modules
    )
