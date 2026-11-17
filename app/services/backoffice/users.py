"""Usuarios del BackOffice (BACKOFFICE_DEALERS.spec.md, secciones 5.2, 5.11 y 6.5).

Se adelantó en la fase B2 lo que necesita la CLI (`create-admin`, `reset-password`): alta con validación y unicidad,
y contraseña temporal. La fase B4 agrega la lista, el detalle, la edición y las acciones (activar, desactivar,
desbloquear, contraseña temporal, eliminar) de POST/GET/PUT/DELETE /users.

Decisiones del responsable (sesión 3, sección 4.1 del spec):
- `version` sube solo al editar los datos del formulario (PUT /users y PATCH /profile).
- `updated_at` y `updated_by` cambian solo con una acción de una persona sobre la ficha (edición o acción); se fijan
  aquí de forma explícita (`User.updated_at` no tiene `onupdate`). El login, el bloqueo automático y los cambios de
  contraseña del propio usuario no los tocan.
- Una edición sin cambios reales no sube `version`, no toca `updated_*` y no deja bitácora. `activate`, `deactivate` y
  `unlock` son idempotentes: sin cambio no hay bitácora.

Orden de bloqueo: el advisory lock de los Administradores (si la operación puede dejar el sistema sin ninguno), luego
la fila del usuario (`FOR UPDATE`) y al final sus filas dependientes (sesiones, enlaces).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwords
from app.core.backoffice_security import (
    LAST_ADMIN,
    SELF_ACTION_NOT_ALLOWED,
    BackofficeError,
    ErrorCode,
    not_found,
    version_conflict,
)
from app.core.config import Settings
from app.models.audit_log import AuditAction, AuditEntityType
from app.models.role import Role, RoleCode
from app.models.user import User, UserStatus
from app.models.user_session import SessionRevokedReason
from app.schemas.backoffice.auth import CodeName
from app.schemas.backoffice.common import UserRef
from app.schemas.backoffice.users import (
    UserCreateRequest,
    UserDetail,
    UserListQuery,
    UserSummary,
    UserUpdateRequest,
)
from app.services.backoffice.audit import AuditContext, diff_changes, record_audit
from app.services.backoffice.listing import contains_any, paginate
from app.services.backoffice.password_reset import invalidate_pending_reset_tokens
from app.services.backoffice.refs import user_refs
from app.services.backoffice.sessions import revoke_user_sessions
from app.utils.datetimes import to_kizuna_iso

logger = logging.getLogger(__name__)

# details.source de USER_CREATED y USER_PASSWORD_RESET_BY_ADMIN (sección 5.10).
SOURCE_API = "API"
SOURCE_CLI = "CLI"

USER_NOT_FOUND = "No existe el usuario."

# Regla del último Administrador (sección 6.5): serializa cambio de rol, desactivar y eliminar.
_ADMINS_LOCK = text("SELECT pg_advisory_xact_lock(hashtext('backoffice:admins'))")

# Unicidad entre usuarios vigentes (Anexo A.3): campo → (índice parcial, mensaje). En el orden en que se valida.
_UNIQUE_FIELDS = {
    "dni": ("uq_users_active_dni", "Ya existe un usuario con ese DNI."),
    "username": ("uq_users_active_username", "Ya existe un usuario con ese nombre de usuario."),
    "email": ("uq_users_active_email", "Ya existe un usuario con ese correo."),
}

# Datos editables (PUT /users, PATCH /profile): campo JSON (camelCase, el de `changes` en la bitácora) → atributo.
EDITABLE_FIELDS = {
    "dni": "dni",
    "firstNames": "first_names",
    "lastNames": "last_names",
    "email": "email",
    "phone": "phone",
    "roleCode": "role_code",
}

_SORT_COLUMNS = {
    "lastNames": (User.last_names, User.first_names),
    "username": (User.username,),
    "createdAt": (User.created_at,),
    "lastLoginAt": (User.last_login_at,),
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


async def flush_or_duplicate(db: AsyncSession) -> None:
    """`flush`; si un índice único parcial lo rechaza (carrera), revierte y responde el mismo 409 DUPLICATE."""
    try:
        await db.flush()
    except IntegrityError as exc:
        duplicate = duplicate_from_integrity_error(exc)
        await db.rollback()
        if duplicate is None:
            raise
        raise duplicate from None


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
    await flush_or_duplicate(db)

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


def _touch(user: User, actor_user_id: uuid.UUID | None, now: datetime) -> None:
    """Acción de una persona sobre la ficha: `updated_at` y `updated_by` (nulo si actúa la CLI)."""
    user.updated_at = now
    user.updated_by = actor_user_id


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
    _touch(user, actor_user_id, now)
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


# --- Lectura (fase B4) ---


def user_status(user: User, now: datetime) -> UserStatus:
    """INACTIVO si `is_active = false`; si no, BLOQUEADO si `locked_until > now`; si no, ACTIVO (sección 6.5)."""
    if not user.is_active:
        return UserStatus.INACTIVO
    if user.locked_until is not None and user.locked_until > now:
        return UserStatus.BLOQUEADO
    return UserStatus.ACTIVO


def user_values(user: User) -> dict[str, Any]:
    """Datos editables en camelCase, para calcular `changes` (sección 5.10)."""
    return {field: getattr(user, attribute) for field, attribute in EDITABLE_FIELDS.items()}


async def list_roles(db: AsyncSession) -> list[Role]:
    return list(await db.scalars(select(Role).order_by(Role.code)))


def build_user_summary(user: User, role: Role, *, zone: ZoneInfo, now: datetime) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        first_names=user.first_names,
        last_names=user.last_names,
        dni=user.dni,
        email=user.email,
        role=CodeName(code=role.code, name=role.name),
        status=user_status(user, now),
        last_login_at=to_kizuna_iso(user.last_login_at, zone),
    )


def build_user_detail(
    user: User, role: Role, refs: dict[uuid.UUID, UserRef], *, zone: ZoneInfo, now: datetime
) -> UserDetail:
    summary = build_user_summary(user, role, zone=zone, now=now)
    return UserDetail(
        **summary.model_dump(),
        phone=user.phone,
        must_change_password=user.must_change_password,
        locked_until=to_kizuna_iso(user.locked_until, zone),
        created_at=to_kizuna_iso(user.created_at, zone) or "",
        created_by=refs.get(user.created_by) if user.created_by else None,
        updated_at=to_kizuna_iso(user.updated_at, zone) or "",
        updated_by=refs.get(user.updated_by) if user.updated_by else None,
        version=user.version,
    )


async def get_user_detail(db: AsyncSession, user_id: uuid.UUID, *, zone: ZoneInfo) -> UserDetail:
    """Usuario vigente; inexistente o eliminado → 404 NOT_FOUND."""
    row = (
        await db.execute(
            select(User, Role)
            .join(Role, Role.code == User.role_code)
            .where(User.id == user_id, User.is_deleted == 0)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        await _fail(db, not_found(USER_NOT_FOUND))
    user, role = row
    refs = await user_refs(db, (user.created_by, user.updated_by))
    detail = build_user_detail(user, role, refs, zone=zone, now=datetime.now(UTC))
    # Solo lectura. Se cierra con commit: un rollback expiraría los objetos de la sesión.
    await db.commit()
    return detail


async def list_users(
    db: AsyncSession, query: UserListQuery, *, zone: ZoneInfo
) -> tuple[list[UserSummary], int]:
    """GET /users: vigentes, con búsqueda, filtros, orden y paginación (sección 6.5)."""
    now = datetime.now(UTC)
    statement = select(User, Role).join(Role, Role.code == User.role_code).where(User.is_deleted == 0)
    if query.q:
        statement = statement.where(
            contains_any(
                query.q,
                User.dni,
                User.first_names,
                User.last_names,
                User.first_names + " " + User.last_names,
                User.username,
                User.email,
            )
        )
    if query.role_code is not None:
        statement = statement.where(User.role_code == query.role_code.value)
    if query.status is UserStatus.INACTIVO:
        statement = statement.where(User.is_active.is_(False))
    elif query.status is UserStatus.BLOQUEADO:
        statement = statement.where(User.is_active.is_(True), User.locked_until > now)
    elif query.status is UserStatus.ACTIVO:
        statement = statement.where(
            User.is_active.is_(True), or_(User.locked_until.is_(None), User.locked_until <= now)
        )

    descending = query.sort_order == "desc"
    order = [column.desc() if descending else column.asc() for column in _SORT_COLUMNS[query.sort_by]]
    if query.sort_by == "lastLoginAt":
        order = [clause.nulls_last() for clause in order]
    # Desempate por id: la paginación es estable aunque haya valores repetidos.
    statement = statement.order_by(*order, User.id.asc())

    rows, total = await paginate(db, statement, page=query.page, page_size=query.page_size)
    items = [build_user_summary(user, role, zone=zone, now=now) for user, role in rows]
    await db.commit()
    return items, total


# --- Edición y acciones (fase B4) ---


async def _fail(db: AsyncSession, error: BackofficeError) -> NoReturn:
    await db.rollback()
    raise error


async def _lock_admins(db: AsyncSession) -> None:
    await db.execute(_ADMINS_LOCK)


async def _locked_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Usuario vigente con `FOR UPDATE`; inexistente o eliminado → 404."""
    user = await db.scalar(
        select(User)
        .where(User.id == user_id, User.is_deleted == 0)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        await _fail(db, not_found(USER_NOT_FOUND))
    return user


async def _ensure_not_self(db: AsyncSession, user: User, actor_user_id: uuid.UUID) -> None:
    if user.id == actor_user_id:
        await _fail(db, BackofficeError(409, ErrorCode.SELF_ACTION_NOT_ALLOWED, SELF_ACTION_NOT_ALLOWED))


async def _ensure_not_last_admin(db: AsyncSession, user: User) -> None:
    """Con el advisory lock ya tomado: cuenta los Administradores activos y vigentes distintos del afectado; si no
    queda ninguno → 409 LAST_ADMIN. Solo aplica si el afectado es Administrador."""
    if user.role_code != RoleCode.ADMIN:
        return
    others = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.role_code == RoleCode.ADMIN.value,
            User.is_active.is_(True),
            User.is_deleted == 0,
            User.id != user.id,
        )
    )
    if not others:
        await _fail(db, BackofficeError(409, ErrorCode.LAST_ADMIN, LAST_ADMIN))


def _record(
    db: AsyncSession,
    action: AuditAction,
    user: User,
    *,
    actor_user_id: uuid.UUID,
    context: AuditContext,
    details: dict[str, Any] | None = None,
) -> None:
    record_audit(
        db,
        action,
        context=context,
        actor_user_id=actor_user_id,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        details=details,
    )


async def update_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: UserUpdateRequest,
    *,
    actor_user_id: uuid.UUID,
    context: AuditContext,
) -> None:
    """PUT /users/{userId}. Chequeos en orden: 404 → VERSION_CONFLICT → SELF_ACTION_NOT_ALLOWED (cambiar el propio
    rol) → LAST_ADMIN (quitar el rol ADMIN al último activo) → DUPLICATE. Hace commit."""
    now = datetime.now(UTC)
    if data.role_code is not RoleCode.ADMIN:
        # Puede quitarle el rol a un Administrador: el lock va primero (sección 6.5).
        await _lock_admins(db)
    user = await _locked_user(db, user_id)
    if user.version != data.version:
        await _fail(db, version_conflict())
    if data.role_code.value != user.role_code:
        await _ensure_not_self(db, user, actor_user_id)
        if data.role_code is not RoleCode.ADMIN:
            await _ensure_not_last_admin(db, user)

    changes = diff_changes(
        user_values(user),
        {
            "dni": data.dni,
            "firstNames": data.first_names,
            "lastNames": data.last_names,
            "email": data.email,
            "phone": data.phone,
            "roleCode": data.role_code.value,
        },
    )
    if not changes:
        await db.rollback()
        return
    unique = {field: change["to"] for field, change in changes.items() if field in ("dni", "email")}
    if unique:
        try:
            await ensure_unique_user_fields(db, exclude_user_id=user.id, **unique)
        except BackofficeError as exc:
            await _fail(db, exc)

    for field, change in changes.items():
        setattr(user, EDITABLE_FIELDS[field], change["to"])
    user.version += 1
    _touch(user, actor_user_id, now)
    await flush_or_duplicate(db)
    _record(
        db,
        AuditAction.USER_UPDATED,
        user,
        actor_user_id=actor_user_id,
        context=context,
        details={"changes": changes},
    )
    await db.commit()
    logger.info(
        "Usuario del BackOffice actualizado",
        extra={"user_id": str(user.id), "actor_user_id": str(actor_user_id)},
    )


async def activate_user(
    db: AsyncSession, user_id: uuid.UUID, *, actor_user_id: uuid.UUID, context: AuditContext
) -> None:
    """Idempotente: un usuario ya activo queda igual, sin bitácora."""
    user = await _locked_user(db, user_id)
    if user.is_active:
        await db.rollback()
        return
    user.is_active = True
    _touch(user, actor_user_id, datetime.now(UTC))
    _record(db, AuditAction.USER_ACTIVATED, user, actor_user_id=actor_user_id, context=context)
    await db.commit()
    logger.info("Usuario del BackOffice activado", extra={"user_id": str(user.id)})


async def deactivate_user(
    db: AsyncSession, user_id: uuid.UUID, *, actor_user_id: uuid.UUID, context: AuditContext
) -> None:
    """No sobre sí mismo; no el último Administrador activo. Revoca sus sesiones (USER_DEACTIVATED). Idempotente:
    un usuario ya inactivo queda igual, sin bitácora."""
    now = datetime.now(UTC)
    await _lock_admins(db)
    user = await _locked_user(db, user_id)
    await _ensure_not_self(db, user, actor_user_id)
    if not user.is_active:
        await db.rollback()
        return
    await _ensure_not_last_admin(db, user)
    user.is_active = False
    _touch(user, actor_user_id, now)
    await revoke_user_sessions(db, user.id, SessionRevokedReason.USER_DEACTIVATED, now)
    _record(db, AuditAction.USER_DEACTIVATED, user, actor_user_id=actor_user_id, context=context)
    await db.commit()
    logger.info("Usuario del BackOffice desactivado", extra={"user_id": str(user.id)})


async def unlock_user(
    db: AsyncSession, user_id: uuid.UUID, *, actor_user_id: uuid.UUID, context: AuditContext
) -> None:
    """`failed_login_attempts = 0`, `locked_until = null`. Sin cambio (sin bloqueo ni intentos) no hay bitácora."""
    user = await _locked_user(db, user_id)
    if user.failed_login_attempts == 0 and user.locked_until is None:
        await db.rollback()
        return
    user.failed_login_attempts = 0
    user.locked_until = None
    _touch(user, actor_user_id, datetime.now(UTC))
    _record(db, AuditAction.USER_UNLOCKED, user, actor_user_id=actor_user_id, context=context)
    await db.commit()
    logger.info("Usuario del BackOffice desbloqueado", extra={"user_id": str(user.id)})


async def reset_password_by_admin(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
    settings: Settings,
    context: AuditContext,
) -> str:
    """POST /users/{userId}/reset-password: no sobre sí mismo (para eso está Cambio de contraseña). Devuelve la
    contraseña temporal, que se muestra una sola vez."""
    user = await _locked_user(db, user_id)
    await _ensure_not_self(db, user, actor_user_id)
    temporary_password = await reset_temporary_password(
        db, user, settings=settings, context=context, actor_user_id=actor_user_id, source=SOURCE_API
    )
    await db.commit()
    return temporary_password


async def delete_user(
    db: AsyncSession, user_id: uuid.UUID, *, actor_user_id: uuid.UUID, context: AuditContext
) -> None:
    """Borrado lógico: no sobre sí mismo; no el último Administrador activo. Revoca sus sesiones (USER_DELETED)."""
    now = datetime.now(UTC)
    await _lock_admins(db)
    user = await _locked_user(db, user_id)
    await _ensure_not_self(db, user, actor_user_id)
    await _ensure_not_last_admin(db, user)
    user.is_deleted = 1
    user.deleted_at = now
    user.deleted_by = actor_user_id
    _touch(user, actor_user_id, now)
    await revoke_user_sessions(db, user.id, SessionRevokedReason.USER_DELETED, now)
    _record(
        db,
        AuditAction.USER_DELETED,
        user,
        actor_user_id=actor_user_id,
        context=context,
        details={"username": user.username},
    )
    await db.commit()
    logger.info("Usuario del BackOffice eliminado", extra={"user_id": str(user.id)})
