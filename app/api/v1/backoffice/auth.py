"""Autenticación y recuperación de contraseña del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.3).

Los correos se envían en BackgroundTasks, después del commit y de responder.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status

from app.core.backoffice_security import (
    BACKOFFICE_PREFIX,
    RESET_LINK_NOT_FOUND,
    SESSION_COOKIE_NAME,
    BackofficeError,
    ErrorCode,
    SessionDep,
    SettingsDep,
    clear_session_cookie,
    get_current_session_allowing_password_change,
    set_session_cookie,
)
from app.core.passwords import PASSWORD_MAX_LENGTH
from app.integrations.email import EmailSender, get_email_sender
from app.schemas.backoffice.auth import (
    ChangePasswordRequest,
    DetailResponse,
    ForgotPasswordRequest,
    LoginRequest,
    Me,
    PasswordPolicyResponse,
    PasswordResetRequest,
    ResetTokenStatusResponse,
)
from app.schemas.backoffice.common import error_responses
from app.services.backoffice import auth as auth_service
from app.services.backoffice import password_reset as reset_service
from app.services.backoffice.audit import AuditContextDep
from app.services.backoffice.sessions import AuthenticatedSession

router = APIRouter(prefix=f"{BACKOFFICE_PREFIX}/auth", tags=["backoffice-auth"])

PendingChangeSession = Annotated[AuthenticatedSession, Depends(get_current_session_allowing_password_change)]
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]
_errors = error_responses


@router.post(
    "/login",
    response_model=Me,
    summary="Iniciar sesión",
    responses=_errors(401, 403, 423),
)
async def login(
    body: LoginRequest, response: Response, db: SessionDep, settings: SettingsDep, context: AuditContextDep
) -> Me:
    """Sección 5.5. Responde el usuario (`Me`) y la cookie `__session`.

    Errores: 401 INVALID_CREDENTIALS (usuario inexistente o contraseña incorrecta), 423 ACCOUNT_LOCKED, 403
    USER_INACTIVE, 403 CSRF_HEADER_MISSING."""
    token, current = await auth_service.login(
        db, username=body.username, password=body.password, settings=settings, context=context
    )
    set_session_cookie(response, token, settings)
    return auth_service.build_me(current, settings.kizuna_zone)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar sesión",
    responses=_errors(403),
)
async def logout(
    request: Request, db: SessionDep, settings: SettingsDep, context: AuditContextDep
) -> Response:
    """Revoca la sesión actual y borra la cookie. Idempotente: sin sesión vigente también responde 204."""
    await auth_service.logout(
        db, request.cookies.get(SESSION_COOKIE_NAME), settings=settings, context=context
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.get("/me", response_model=Me, summary="Usuario con sesión", responses=_errors(401))
async def me(current: PendingChangeSession, settings: SettingsDep) -> Me:
    """También responde con un cambio de contraseña pendiente (`mustChangePassword: true`)."""
    return auth_service.build_me(current, settings.kizuna_zone)


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cambiar la contraseña",
    responses=_errors(401, 403, 422),
)
async def change_password(
    body: ChangePasswordRequest,
    current: PendingChangeSession,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
    sender: EmailSenderDep,
) -> Response:
    """Cierra las demás sesiones y rota la actual (nueva cookie). 422 CURRENT_PASSWORD_INVALID o PASSWORD_POLICY.

    Después del commit envía el correo "Tu contraseña fue actualizada"."""
    token, changed = await auth_service.change_password(
        db,
        current,
        current_password=body.current_password,
        new_password=body.new_password,
        settings=settings,
        context=context,
    )
    changed_at = changed.user.password_changed_at
    assert changed_at is not None
    background_tasks.add_task(
        sender.send, reset_service.password_changed_message(changed.user, changed_at, settings)
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_session_cookie(response, token, settings)
    return response


@router.get("/password-policy", response_model=PasswordPolicyResponse, summary="Política de contraseñas")
async def password_policy(settings: SettingsDep) -> PasswordPolicyResponse:
    """Para el checklist del frontend. Público."""
    return PasswordPolicyResponse(
        min_length=settings.backoffice_password_min_length,
        max_length=PASSWORD_MAX_LENGTH,
        require_uppercase=True,
        require_lowercase=True,
        require_digit=True,
        require_special=True,
    )


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DetailResponse,
    summary="Pedir un enlace para restablecer la contraseña",
    responses=_errors(403),
)
async def forgot_password(
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
    sender: EmailSenderDep,
) -> DetailResponse:
    """Siempre 202 con el mismo cuerpo, exista o no el usuario (no revela cuentas). `identifier`: usuario o correo."""
    message = await reset_service.request_password_reset(
        db, body.identifier, settings=settings, context=context
    )
    if message is not None:
        background_tasks.add_task(sender.send, message)
    return DetailResponse(detail=reset_service.forgot_password_detail(settings))


@router.get(
    "/password-reset/{token}",
    response_model=ResetTokenStatusResponse,
    summary="Estado de un enlace de restablecimiento",
    responses=_errors(404),
)
async def password_reset_status(token: str, db: SessionDep) -> ResetTokenStatusResponse:
    """`tokenStatus`: VALID, EXPIRED, USED o INVALID (invalidado por uno más nuevo o usuario no activo). No devuelve
    datos del usuario. Token inexistente → 404 RESET_LINK_NOT_FOUND."""
    token_status = await reset_service.get_reset_token_status(db, token)
    if token_status is None:
        raise BackofficeError(404, ErrorCode.RESET_LINK_NOT_FOUND, RESET_LINK_NOT_FOUND)
    return ResetTokenStatusResponse(token_status=token_status.value)


@router.post(
    "/password-reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Restablecer la contraseña con el enlace",
    responses=_errors(403, 404, 410, 422),
)
async def password_reset(
    body: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
    sender: EmailSenderDep,
) -> Response:
    """Cambia la contraseña, desbloquea al usuario y cierra todas sus sesiones. No inicia sesión. 404
    RESET_LINK_NOT_FOUND, 410 RESET_LINK_INVALID (vencido, usado, invalidado o usuario no activo), 422 PASSWORD_POLICY.
    Después del commit envía el correo "Tu contraseña fue actualizada"."""
    message = await reset_service.reset_password(
        db, body.token, body.new_password, settings=settings, context=context
    )
    background_tasks.add_task(sender.send, message)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
