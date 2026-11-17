"""Mi perfil (BACKOFFICE_DEALERS.spec.md, sección 6.4). Cualquier usuario con sesión (sin módulo)."""

from fastapi import APIRouter

from app.core.backoffice_security import BACKOFFICE_PREFIX, CurrentSession, SessionDep, SettingsDep
from app.schemas.backoffice.auth import Me
from app.schemas.backoffice.common import error_responses
from app.schemas.backoffice.users import ProfileUpdateRequest
from app.services.backoffice import auth as auth_service
from app.services.backoffice import profile as profile_service
from app.services.backoffice.audit import AuditContextDep

router = APIRouter(prefix=f"{BACKOFFICE_PREFIX}/profile", tags=["backoffice-users"])


@router.get("", response_model=Me, summary="Mi perfil", responses=error_responses(401, 403))
async def get_profile(current: CurrentSession, settings: SettingsDep) -> Me:
    """Mismo cuerpo que `GET /auth/me`."""
    return auth_service.build_me(current, settings.kizuna_zone)


@router.patch(
    "", response_model=Me, summary="Editar mi perfil", responses=error_responses(401, 403, 409, 422)
)
async def update_profile(
    body: ProfileUpdateRequest,
    current: CurrentSession,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
) -> Me:
    """Nombres, apellidos, correo y celular (`phone: null` lo borra). Cambiar el correo exige `currentPassword` (422
    CURRENT_PASSWORD_INVALID); correo de otro usuario → 409 DUPLICATE. `dni`, `username` o `roleCode` → 422."""
    updated = await profile_service.update_profile(db, current, body, context=context)
    return auth_service.build_me(updated, settings.kizuna_zone)
