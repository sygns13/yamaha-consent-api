"""Recuperación de contraseña por correo (BACKOFFICE_DEALERS.spec.md, secciones 4.3, 6.3 y 7).

- `request_password_reset` (POST /auth/forgot-password): nunca revela si el usuario existe; el endpoint responde
  siempre el mismo 202.
- `get_reset_token_status` (GET /auth/password-reset/{token}).
- `reset_password` (POST /auth/password-reset).

En la base solo va el SHA-256 del token; el token en claro solo existe en el correo. Orden de bloqueo en todo el
BackOffice: primero la fila del usuario y luego sus enlaces (evita bloqueos mutuos con forgot-password y con el
cambio de contraseña).
"""

import enum
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwords
from app.core.backoffice_security import (
    RESET_LINK_INVALID,
    RESET_LINK_NOT_FOUND,
    BackofficeError,
    ErrorCode,
    minutes_text,
)
from app.core.config import Settings
from app.integrations.email import EmailMessageData
from app.models.audit_log import AuditAction, AuditEntityType
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.models.user_session import SessionRevokedReason
from app.services.backoffice.audit import AuditContext, record_audit
from app.services.backoffice.email_templates import password_changed_email, password_reset_email
from app.services.backoffice.sessions import revoke_user_sessions
from app.utils.tokens import generate_token, hash_token

logger = logging.getLogger(__name__)

RESET_PATH = "/restablecer-contrasena"
FORGOT_PASSWORD_DETAIL = (
    "Si los datos corresponden a un usuario activo, te enviaremos un correo con un enlace para restablecer tu "
    "contraseña. El enlace vence en {ttl}."
)


class TokenStatus(enum.StrEnum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    USED = "USED"
    # Invalidado por un enlace más nuevo o por un cambio de contraseña, o el usuario ya no está activo.
    INVALID = "INVALID"


def forgot_password_detail(settings: Settings) -> str:
    """Cuerpo único del 202 (el número de minutos sale de BACKOFFICE_PASSWORD_RESET_TTL_MINUTES)."""
    return FORGOT_PASSWORD_DETAIL.format(ttl=minutes_text(settings.backoffice_password_reset_ttl_minutes))


async def invalidate_pending_reset_tokens(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> int:
    """Marca `invalidated_at` en los enlaces sin usar del usuario. `password_reset_tokens.invalidated_at` "se marca al
    pedir un enlace nuevo o al cambiar la contraseña" (sección 4.3)."""
    result = await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def password_changed_message(user: User, changed_at: datetime, settings: Settings) -> EmailMessageData:
    """Aviso "Tu contraseña fue actualizada" (change-password y password-reset)."""
    subject, text, html = password_changed_email(
        first_names=user.first_names, username=user.username, changed_at=changed_at, zone=settings.kizuna_zone
    )
    return EmailMessageData(to=user.email, subject=subject, text=text, html=html)


async def request_password_reset(
    db: AsyncSession, identifier: str, *, settings: Settings, context: AuditContext
) -> EmailMessageData | None:
    """Sección 6.3, pasos 1–3. Devuelve el correo a enviar después del commit, o None si no corresponde enviar nada
    (usuario inexistente, eliminado o inactivo, o límite por hora alcanzado). Un usuario bloqueado sí puede pedirlo."""
    now = datetime.now(UTC)
    normalized = identifier.strip().lower()
    # Usuario o correo: un usuario no puede tener "@", así que a lo más coincide un usuario vigente.
    user = await db.scalar(
        select(User)
        .where(User.is_deleted == 0, or_(User.username == normalized, User.email == normalized))
        .with_for_update()
    )
    if user is None or not user.is_active:
        await db.rollback()
        return None

    recent = await db.scalar(
        select(func.count())
        .select_from(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.created_at > now - timedelta(hours=1)
        )
    )
    if (recent or 0) >= settings.backoffice_password_reset_max_per_hour:
        user_id = str(user.id)  # el rollback expira el objeto
        await db.rollback()
        logger.warning(
            "Límite de enlaces de restablecimiento por hora alcanzado: no se envía otro",
            extra={"user_id": user_id},
        )
        return None

    await invalidate_pending_reset_tokens(db, user.id, now)
    token, token_hash = generate_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            created_at=now,
            expires_at=now + timedelta(minutes=settings.backoffice_password_reset_ttl_minutes),
            requested_ip=context.ip_address,
            requested_user_agent=context.user_agent,
        )
    )
    # Acción anónima (sin sesión): actor nulo (sección 4.3).
    record_audit(
        db,
        AuditAction.AUTH_PASSWORD_RESET_REQUESTED,
        context=context,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
    )
    await db.commit()
    logger.info("Enlace de restablecimiento generado", extra={"user_id": str(user.id)})

    subject, text, html = password_reset_email(
        first_names=user.first_names,
        username=user.username,
        link=f"{settings.backoffice_base_url}{RESET_PATH}?t={token}",
        ttl_minutes=settings.backoffice_password_reset_ttl_minutes,
    )
    return EmailMessageData(to=user.email, subject=subject, text=text, html=html)


def _token_status(reset_token: PasswordResetToken, user: User, now: datetime) -> TokenStatus:
    """Si aplican varios estados: USED > INVALID > EXPIRED (mismo criterio que la landing: usado gana a vencido)."""
    if reset_token.used_at is not None:
        return TokenStatus.USED
    if reset_token.invalidated_at is not None or user.is_deleted or not user.is_active:
        return TokenStatus.INVALID
    if reset_token.expires_at <= now:
        return TokenStatus.EXPIRED
    return TokenStatus.VALID


async def get_reset_token_status(db: AsyncSession, token: str) -> TokenStatus | None:
    """Estado del enlace; None si el token no existe. No devuelve datos del usuario."""
    row = (
        await db.execute(
            select(PasswordResetToken, User)
            .join(User, User.id == PasswordResetToken.user_id)
            .where(PasswordResetToken.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        return None
    return _token_status(row[0], row[1], datetime.now(UTC))


async def reset_password(
    db: AsyncSession, token: str, new_password: str, *, settings: Settings, context: AuditContext
) -> EmailMessageData:
    """Sección 6.3, POST /auth/password-reset, pasos 1–4. Devuelve el aviso a enviar después del commit.

    El token se bloquea (`FOR UPDATE`) antes de validar su estado: de dos envíos simultáneos, el segundo espera y
    recibe 410. Se bloquea primero el usuario y luego el token (mismo orden que forgot-password).
    """
    now = datetime.now(UTC)
    token_hash = hash_token(token)
    user_id = await db.scalar(
        select(PasswordResetToken.user_id).where(PasswordResetToken.token_hash == token_hash)
    )
    if user_id is None:
        await db.rollback()
        raise BackofficeError(404, ErrorCode.RESET_LINK_NOT_FOUND, RESET_LINK_NOT_FOUND)

    user = await db.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    reset_token = await db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert user is not None and reset_token is not None  # FK y token sin borrar en la misma transacción
    if _token_status(reset_token, user, now) is not TokenStatus.VALID:
        await db.rollback()
        raise BackofficeError(410, ErrorCode.RESET_LINK_INVALID, RESET_LINK_INVALID)

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

    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = now
    user.failed_login_attempts = 0
    user.locked_until = None
    reset_token.used_at = now
    await db.flush()
    await invalidate_pending_reset_tokens(db, user.id, now)
    await revoke_user_sessions(db, user.id, SessionRevokedReason.PASSWORD_RESET, now)
    # Acción sin sesión: actor nulo.
    record_audit(
        db,
        AuditAction.AUTH_PASSWORD_RESET_COMPLETED,
        context=context,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
    )
    await db.commit()
    logger.info("Contraseña restablecida por correo", extra={"user_id": str(user.id)})
    return password_changed_message(user, now, settings)
