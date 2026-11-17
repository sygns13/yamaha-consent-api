"""Usuarios del BackOffice y catálogo de roles (BACKOFFICE_DEALERS.spec.md, sección 6.5). Módulo `USUARIOS`.

Las respuestas de las mutaciones se arman leyendo de nuevo el usuario después del commit.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.backoffice_security import BACKOFFICE_PREFIX, SessionDep, SettingsDep, require_module
from app.models.role import ModuleCode
from app.schemas.backoffice.auth import CodeName
from app.schemas.backoffice.common import Page, error_responses
from app.schemas.backoffice.users import (
    TemporaryPasswordResponse,
    UserCreatedResponse,
    UserCreateRequest,
    UserDetail,
    UserListQuery,
    UserSummary,
    UserUpdateRequest,
)
from app.services.backoffice import users as users_service
from app.services.backoffice.audit import AuditContextDep
from app.services.backoffice.sessions import AuthenticatedSession

router = APIRouter(prefix=BACKOFFICE_PREFIX, tags=["backoffice-users"])

UsersModule = Annotated[AuthenticatedSession, Depends(require_module(ModuleCode.USUARIOS))]
UserId = Annotated[uuid.UUID, Path(alias="userId")]

# 401 sin sesión, 403 sin el módulo (o con cambio de contraseña pendiente).
_SESSION_ERRORS = (401, 403)


@router.get(
    "/roles",
    response_model=list[CodeName],
    summary="Roles (tipos de usuario)",
    responses=error_responses(*_SESSION_ERRORS),
)
async def list_roles(_current: UsersModule, db: SessionDep) -> list[CodeName]:
    roles = await users_service.list_roles(db)
    return [CodeName(code=role.code, name=role.name) for role in roles]


@router.get(
    "/users",
    response_model=Page[UserSummary],
    summary="Lista de usuarios",
    responses=error_responses(*_SESSION_ERRORS),
)
async def list_users(
    query: Annotated[UserListQuery, Query()], _current: UsersModule, db: SessionDep, settings: SettingsDep
) -> Page[UserSummary]:
    """Excluye los eliminados. `q`: DNI, nombres, apellidos, nombre completo, usuario o correo. `status`: ACTIVO,
    INACTIVO o BLOQUEADO (derivado). `sortBy`: lastNames (apellidos y nombres), username, createdAt, lastLoginAt."""
    items, total = await users_service.list_users(db, query, zone=settings.kizuna_zone)
    return Page[UserSummary](items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    response_model=UserCreatedResponse,
    summary="Crear un usuario",
    responses=error_responses(*_SESSION_ERRORS, 409),
)
async def create_user(
    body: UserCreateRequest,
    current: UsersModule,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
) -> UserCreatedResponse:
    """Crea el usuario con una contraseña temporal (se muestra una sola vez) y cambio obligatorio en el primer
    ingreso. 409 DUPLICATE (DNI, usuario o correo) con su `field`."""
    user, temporary_password = await users_service.create_user(
        db, body, settings=settings, context=context, actor_user_id=current.user.id
    )
    await db.commit()
    detail = await users_service.get_user_detail(db, user.id, zone=settings.kizuna_zone)
    return UserCreatedResponse(user=detail, temporary_password=temporary_password)


@router.get(
    "/users/{userId}",
    response_model=UserDetail,
    summary="Detalle de un usuario",
    responses=error_responses(*_SESSION_ERRORS, 404),
)
async def get_user(
    user_id: UserId, _current: UsersModule, db: SessionDep, settings: SettingsDep
) -> UserDetail:
    return await users_service.get_user_detail(db, user_id, zone=settings.kizuna_zone)


@router.put(
    "/users/{userId}",
    response_model=UserDetail,
    summary="Editar un usuario",
    responses=error_responses(*_SESSION_ERRORS, 404, 409),
)
async def update_user(
    user_id: UserId,
    body: UserUpdateRequest,
    current: UsersModule,
    db: SessionDep,
    settings: SettingsDep,
    context: AuditContextDep,
) -> UserDetail:
    """`username` no se edita. 409 VERSION_CONFLICT, SELF_ACTION_NOT_ALLOWED (cambiar el propio rol), LAST_ADMIN o
    DUPLICATE."""
    await users_service.update_user(db, user_id, body, actor_user_id=current.user.id, context=context)
    return await users_service.get_user_detail(db, user_id, zone=settings.kizuna_zone)


@router.delete(
    "/users/{userId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un usuario",
    responses=error_responses(*_SESSION_ERRORS, 404, 409),
)
async def delete_user(
    user_id: UserId, current: UsersModule, db: SessionDep, context: AuditContextDep
) -> Response:
    """Borrado lógico; cierra sus sesiones. No sobre sí mismo ni sobre el último Administrador activo."""
    await users_service.delete_user(db, user_id, actor_user_id=current.user.id, context=context)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{userId}/activate",
    response_model=UserDetail,
    summary="Activar un usuario",
    responses=error_responses(*_SESSION_ERRORS, 404),
)
async def activate_user(
    user_id: UserId, current: UsersModule, db: SessionDep, settings: SettingsDep, context: AuditContextDep
) -> UserDetail:
    """Idempotente."""
    await users_service.activate_user(db, user_id, actor_user_id=current.user.id, context=context)
    return await users_service.get_user_detail(db, user_id, zone=settings.kizuna_zone)


@router.post(
    "/users/{userId}/deactivate",
    response_model=UserDetail,
    summary="Desactivar un usuario",
    responses=error_responses(*_SESSION_ERRORS, 404, 409),
)
async def deactivate_user(
    user_id: UserId, current: UsersModule, db: SessionDep, settings: SettingsDep, context: AuditContextDep
) -> UserDetail:
    """Cierra sus sesiones. No sobre sí mismo ni sobre el último Administrador activo. Idempotente."""
    await users_service.deactivate_user(db, user_id, actor_user_id=current.user.id, context=context)
    return await users_service.get_user_detail(db, user_id, zone=settings.kizuna_zone)


@router.post(
    "/users/{userId}/unlock",
    response_model=UserDetail,
    summary="Desbloquear un usuario",
    responses=error_responses(*_SESSION_ERRORS, 404),
)
async def unlock_user(
    user_id: UserId, current: UsersModule, db: SessionDep, settings: SettingsDep, context: AuditContextDep
) -> UserDetail:
    """Quita el bloqueo por intentos fallidos. Idempotente."""
    await users_service.unlock_user(db, user_id, actor_user_id=current.user.id, context=context)
    return await users_service.get_user_detail(db, user_id, zone=settings.kizuna_zone)


@router.post(
    "/users/{userId}/reset-password",
    response_model=TemporaryPasswordResponse,
    summary="Generar una contraseña temporal",
    responses=error_responses(*_SESSION_ERRORS, 404, 409),
)
async def reset_password(
    user_id: UserId, current: UsersModule, db: SessionDep, settings: SettingsDep, context: AuditContextDep
) -> TemporaryPasswordResponse:
    """Contraseña temporal (se muestra una sola vez) con cambio obligatorio; desbloquea y cierra sus sesiones. No
    sobre sí mismo (para eso está Cambiar contraseña)."""
    temporary_password = await users_service.reset_password_by_admin(
        db, user_id, actor_user_id=current.user.id, settings=settings, context=context
    )
    return TemporaryPasswordResponse(temporary_password=temporary_password)
