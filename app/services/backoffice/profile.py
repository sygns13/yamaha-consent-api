"""Mi perfil (BACKOFFICE_DEALERS.spec.md, sección 6.4).

El usuario edita sus nombres, apellidos, correo y celular. Cambiar el correo exige la contraseña actual, porque el
correo es el canal de recuperación. `dni`, `username` y el rol no se cambian desde aquí (el schema los rechaza).
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwords
from app.core.backoffice_security import CURRENT_PASSWORD_INVALID, BackofficeError, ErrorCode
from app.models.audit_log import AuditAction, AuditEntityType
from app.models.user import User
from app.schemas.backoffice.users import ProfileUpdateRequest
from app.services.backoffice.audit import AuditContext, diff_changes, record_audit
from app.services.backoffice.sessions import AuthenticatedSession
from app.services.backoffice.users import (
    EDITABLE_FIELDS,
    ensure_unique_user_fields,
    flush_or_duplicate,
    user_values,
)

logger = logging.getLogger(__name__)

# Campo del request (snake_case) → campo de `changes` (camelCase).
_PROFILE_FIELDS = {
    "first_names": "firstNames",
    "last_names": "lastNames",
    "email": "email",
    "phone": "phone",
}


async def update_profile(
    db: AsyncSession, current: AuthenticatedSession, data: ProfileUpdateRequest, *, context: AuditContext
) -> AuthenticatedSession:
    """PATCH /profile. Solo cambian los campos enviados. Sin cambios reales: no sube `version` ni deja bitácora.

    Si cambia el correo: `currentPassword` obligatoria y correcta (si no, 422 CURRENT_PASSWORD_INVALID; no suma
    intentos de login) y se valida antes que la unicidad (409 DUPLICATE), para no revelar correos ajenos a quien no
    conoce la contraseña. Devuelve la sesión con el usuario actualizado (para el cuerpo `Me`). Hace commit."""
    now = datetime.now(UTC)
    user = await db.get(User, current.user.id, with_for_update=True, populate_existing=True)
    assert user is not None  # la sesión acaba de validarse

    sent = {
        field: getattr(data, attribute)
        for attribute, field in _PROFILE_FIELDS.items()
        if attribute in data.model_fields_set
    }
    changes = diff_changes(user_values(user), sent)
    if "email" in changes and (
        data.current_password is None
        or not passwords.verify_password(user.password_hash, data.current_password)
    ):
        await db.rollback()
        raise BackofficeError(
            422, ErrorCode.CURRENT_PASSWORD_INVALID, CURRENT_PASSWORD_INVALID, field="currentPassword"
        )

    if changes:
        if "email" in changes:
            try:
                await ensure_unique_user_fields(db, exclude_user_id=user.id, email=changes["email"]["to"])
            except BackofficeError:
                await db.rollback()
                raise
        for field, change in changes.items():
            setattr(user, EDITABLE_FIELDS[field], change["to"])
        user.version += 1
        user.updated_at = now
        user.updated_by = user.id
        await flush_or_duplicate(db)
        record_audit(
            db,
            AuditAction.PROFILE_UPDATED,
            context=context,
            actor_user_id=user.id,
            entity_type=AuditEntityType.USER,
            entity_id=user.id,
            details={"changes": changes},
        )
        logger.info("Perfil del BackOffice actualizado", extra={"user_id": str(user.id)})
    # Con o sin cambios: commit (un rollback expiraría el usuario que se devuelve).
    await db.commit()
    return AuthenticatedSession(
        session=current.session, user=user, role=current.role, modules=current.modules
    )
