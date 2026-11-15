"""Sesiones del BackOffice (BACKOFFICE_DEALERS.spec.md, secciones 5.3 y 5.4).

Token opaco `secrets.token_urlsafe(32)` en la cookie `__session`; en `user_sessions` solo su SHA-256. Vencen por
inactividad (BACKOFFICE_SESSION_IDLE_MINUTES desde `last_activity_at`) y por máximo (`expires_at`).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.role import BackofficeModule, Role, RoleModule
from app.models.user import User
from app.models.user_session import SessionRevokedReason, UserSession
from app.services.backoffice.audit import AuditContext
from app.utils.tokens import generate_token, hash_token

# last_activity_at se renueva como máximo una vez por minuto (sección 5.4, paso 4).
ACTIVITY_REFRESH_INTERVAL = timedelta(seconds=60)
# Sesiones vencidas o revocadas que se conservan (sección 5.13). La CLI `purge` lo recibe como --sessions-days.
SESSION_RETENTION_DAYS = 30


@dataclass
class AuthenticatedSession:
    """Sesión válida con su usuario, su rol y los módulos del rol (leídos de la base en cada petición)."""

    session: UserSession
    user: User
    role: Role
    modules: list[BackofficeModule]

    @property
    def module_codes(self) -> frozenset[str]:
        return frozenset(module.code for module in self.modules)


def create_session(
    db: AsyncSession, user_id: uuid.UUID, *, settings: Settings, now: datetime, context: AuditContext
) -> tuple[str, UserSession]:
    """Agrega la sesión a `db` y devuelve (token en claro para la cookie, sesión)."""
    token, token_hash = generate_token()
    user_session = UserSession(
        user_id=user_id,
        token_hash=token_hash,
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(hours=settings.backoffice_session_max_hours),
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    db.add(user_session)
    return token, user_session


async def find_session(db: AsyncSession, token: str) -> tuple[UserSession, User] | None:
    """Sesión del token (vigente o no) con su usuario."""
    row = (
        await db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).first()
    return (row[0], row[1]) if row else None


def is_session_expired(user_session: UserSession, *, now: datetime, settings: Settings) -> bool:
    """Revocada, vencida por máximo o por inactividad."""
    idle = timedelta(minutes=settings.backoffice_session_idle_minutes)
    return (
        user_session.revoked_at is not None
        or user_session.expires_at <= now
        or user_session.last_activity_at + idle <= now
    )


def needs_activity_refresh(user_session: UserSession, now: datetime) -> bool:
    return now - user_session.last_activity_at > ACTIVITY_REFRESH_INTERVAL


async def load_role_and_modules(db: AsyncSession, role_code: str) -> tuple[Role, list[BackofficeModule]]:
    """Rol y sus módulos ordenados por sort_order."""
    role = await db.get(Role, role_code)
    if role is None:  # la FK users.role_code lo impide
        raise LookupError(f"Rol inexistente: {role_code}")
    modules = list(
        await db.scalars(
            select(BackofficeModule)
            .join(RoleModule, RoleModule.module_code == BackofficeModule.code)
            .where(RoleModule.role_code == role_code)
            .order_by(BackofficeModule.sort_order)
        )
    )
    return role, modules


def revoke_session(user_session: UserSession, reason: SessionRevokedReason, now: datetime) -> None:
    if user_session.revoked_at is None:
        user_session.revoked_at = now
        user_session.revoked_reason = reason.value


async def revoke_user_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    reason: SessionRevokedReason,
    now: datetime,
    *,
    except_session_id: uuid.UUID | None = None,
) -> int:
    """Revoca las sesiones no revocadas del usuario (menos `except_session_id`). Devuelve cuántas."""
    statement = (
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason.value)
        .execution_options(synchronize_session=False)
    )
    if except_session_id is not None:
        statement = statement.where(UserSession.id != except_session_id)
    result = await db.execute(statement)
    return result.rowcount or 0


def stale_session_condition(cutoff: datetime) -> ColumnElement[bool]:
    """Sesiones revocadas, vencidas o inactivas antes de `cutoff` (sección 5.13). La inactividad máxima se mide en
    minutos: una sesión sin actividad desde antes del corte ya venció hace casi el mismo tiempo."""
    return or_(
        UserSession.revoked_at < cutoff,
        UserSession.expires_at < cutoff,
        UserSession.last_activity_at < cutoff,
    )


async def purge_user_sessions(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    """En cada login correcto: borra las sesiones del usuario vencidas o revocadas hace más de 30 días."""
    cutoff = now - timedelta(days=SESSION_RETENTION_DAYS)
    result = await db.execute(
        delete(UserSession)
        .where(UserSession.user_id == user_id, stale_session_condition(cutoff))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0
