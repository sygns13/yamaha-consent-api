"""Usuarios y roles del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, fase B4): criterios B4.1–B4.9.

Decisiones del responsable (sesión 3): `version` sube solo con PUT /users y PATCH /profile; `updated_at` y
`updated_by` solo con acciones de una persona sobre la ficha; sin cambios no hay bitácora; activate, deactivate y
unlock son idempotentes; `q` también busca el nombre completo. Los tiempos se simulan en la base, sin `sleep`.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.backoffice_security import BackofficeError
from app.models import AuditAction, RoleCode
from app.schemas.backoffice.users import UserUpdateRequest
from app.services.backoffice import users as users_service
from app.services.backoffice.audit import AuditContext
from app.utils.validation import USERNAME_INVALID
from tests.bo_helpers import (
    BO,
    BoUser,
    Sessions,
    audit_rows,
    create_bo_user,
    get_user,
    logged_client,
    login_as,
    reset_tokens,
    update_user,
    user_sessions,
)

USERS = f"{BO}/users"
ISO_LIMA = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-05:00$")
ClientFactory = Callable[[], httpx.AsyncClient]

NEW_USER = {
    "dni": "41234567",
    "firstNames": "Rosa",
    "lastNames": "Huamán Ñique",
    "username": "rhuaman",
    "email": "rhuaman@correo.test",
    "phone": "987111222",
    "roleCode": "OPERADOR",
}
NOT_FOUND = {"detail": "No existe el usuario.", "code": "NOT_FOUND"}
SELF_ACTION = {
    "detail": "No puedes realizar esta acción sobre tu propio usuario.",
    "code": "SELF_ACTION_NOT_ALLOWED",
}
LAST_ADMIN = {"detail": "Debe quedar al menos un Administrador activo.", "code": "LAST_ADMIN"}
VERSION_CONFLICT = {
    "detail": "El registro fue modificado por otro usuario. Recarga la página e inténtalo de nuevo.",
    "code": "VERSION_CONFLICT",
}
FORBIDDEN = {"detail": "No tienes permiso para realizar esta acción.", "code": "FORBIDDEN"}


@pytest.fixture
async def admin_client(make_bo_client: ClientFactory, admin_user: BoUser) -> httpx.AsyncClient:
    return await logged_client(make_bo_client, admin_user)


async def make_user(sessions: Sessions, number: int, **values: Any) -> BoUser:
    """Usuario de prueba con valores únicos derivados de `number`."""
    username = values.pop("username", f"usuario{number:02d}")
    return await create_bo_user(
        sessions,
        username=username,
        dni=values.pop("dni", f"5000{number:04d}"),
        email=values.pop("email", f"{username}@correo.test"),
        **values,
    )


def put_body(detail: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Cuerpo de PUT a partir de un `UserDetail`, con `changes` aplicados."""
    body = {
        "dni": detail["dni"],
        "firstNames": detail["firstNames"],
        "lastNames": detail["lastNames"],
        "email": detail["email"],
        "phone": detail["phone"],
        "roleCode": detail["role"]["code"],
        "version": detail["version"],
    }
    body.update(changes)
    return body


async def detail_of(client: httpx.AsyncClient, user_id: uuid.UUID | str) -> dict[str, Any]:
    response = await client.get(f"{USERS}/{user_id}")
    assert response.status_code == 200, response.text
    return response.json()


def admin_ref(admin: BoUser) -> dict[str, str]:
    return {
        "id": str(admin.id),
        "username": admin.username,
        "fullName": f"{admin.first_names} {admin.last_names}",
    }


# --- B4.9 Acceso: solo el módulo USUARIOS ---

USER_ENDPOINTS = [
    ("GET", "/roles"),
    ("GET", "/users"),
    ("POST", "/users"),
    ("GET", "/users/{id}"),
    ("PUT", "/users/{id}"),
    ("DELETE", "/users/{id}"),
    ("POST", "/users/{id}/activate"),
    ("POST", "/users/{id}/deactivate"),
    ("POST", "/users/{id}/unlock"),
    ("POST", "/users/{id}/reset-password"),
]


@pytest.mark.parametrize(("method", "path"), USER_ENDPOINTS)
async def test_operator_is_forbidden(
    make_bo_client: ClientFactory, operator_user: BoUser, admin_user: BoUser, method: str, path: str
) -> None:
    client = await logged_client(make_bo_client, operator_user)
    response = await client.request(method, f"{BO}{path.format(id=admin_user.id)}")
    assert response.status_code == 403
    assert response.json() == FORBIDDEN


@pytest.mark.parametrize(("method", "path"), USER_ENDPOINTS)
async def test_user_endpoints_require_a_session(bo_client: httpx.AsyncClient, method: str, path: str) -> None:
    response = await bo_client.request(method, f"{BO}{path.format(id=uuid.uuid4())}")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


async def test_pending_password_change_blocks_users_module(
    make_bo_client: ClientFactory, session_factory: Sessions
) -> None:
    admin = await make_user(session_factory, 1, role_code=RoleCode.ADMIN, must_change_password=True)
    client = await logged_client(make_bo_client, admin)
    response = await client.get(USERS)
    assert response.status_code == 403 and response.json()["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_roles(admin_client: httpx.AsyncClient) -> None:
    response = await admin_client.get(f"{BO}/roles")
    assert response.status_code == 200
    assert response.json() == [
        {"code": "ADMIN", "name": "Administrador"},
        {"code": "OPERADOR", "name": "Operador"},
    ]


# --- B4.1 CRUD con bitácora ---


async def test_create_user(
    admin_client: httpx.AsyncClient,
    make_bo_client: ClientFactory,
    admin_user: BoUser,
    session_factory: Sessions,
) -> None:
    response = await admin_client.post(USERS, json=NEW_USER)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"user", "temporaryPassword"}
    temporary = body["temporaryPassword"]
    assert len(temporary) == 20
    user = body["user"]
    created_id = user.pop("id")
    for key in ("createdAt", "updatedAt"):
        assert ISO_LIMA.match(user.pop(key))
    assert user == {
        "username": "rhuaman",
        "firstNames": "Rosa",
        "lastNames": "Huamán Ñique",
        "dni": "41234567",
        "email": "rhuaman@correo.test",
        "role": {"code": "OPERADOR", "name": "Operador"},
        "status": "ACTIVO",
        "lastLoginAt": None,
        "phone": "987111222",
        "mustChangePassword": True,
        "lockedUntil": None,
        "createdBy": admin_ref(admin_user),
        "updatedBy": admin_ref(admin_user),
        "version": 1,
    }

    stored = await get_user(session_factory, uuid.UUID(created_id))
    assert stored.created_by == admin_user.id and stored.updated_by == admin_user.id
    assert stored.password_hash.startswith("$argon2id$") and temporary not in stored.password_hash

    (entry,) = await audit_rows(session_factory, AuditAction.USER_CREATED)
    assert entry.actor_user_id == admin_user.id
    assert entry.entity_type == "USER" and entry.entity_id == created_id
    assert entry.details == {
        "dni": "41234567",
        "firstNames": "Rosa",
        "lastNames": "Huamán Ñique",
        "username": "rhuaman",
        "email": "rhuaman@correo.test",
        "phone": "987111222",
        "roleCode": "OPERADOR",
    }
    assert entry.ip_address is not None and entry.user_agent == "pytest-backoffice"

    # La contraseña temporal sirve una vez para entrar y obliga a cambiarla.
    created = BoUser(
        id=uuid.UUID(created_id),
        username="rhuaman",
        password=temporary,
        dni="41234567",
        email="rhuaman@correo.test",
        first_names="Rosa",
        last_names="Huamán Ñique",
        role_code="OPERADOR",
    )
    login = await login_as(make_bo_client(), created)
    assert login.status_code == 200 and login.json()["mustChangePassword"] is True


async def test_create_user_normalizes_values(admin_client: httpx.AsyncClient) -> None:
    response = await admin_client.post(
        USERS,
        json={
            **NEW_USER,
            "dni": " 41234567 ",
            "firstNames": "  rosa   maría ",
            "username": "  RHuaman ",
            "email": " RHuaman@Correo.TEST ",
            "phone": "+51 987-111-222",
        },
    )
    assert response.status_code == 201, response.text
    user = response.json()["user"]
    assert (user["dni"], user["firstNames"], user["username"], user["email"], user["phone"]) == (
        "41234567",
        "rosa maría",
        "rhuaman",
        "rhuaman@correo.test",
        "987111222",
    )


async def test_create_user_without_phone(admin_client: httpx.AsyncClient) -> None:
    body = {key: value for key, value in NEW_USER.items() if key != "phone"}
    response = await admin_client.post(USERS, json=body)
    assert response.status_code == 201 and response.json()["user"]["phone"] is None
    response = await admin_client.post(
        USERS,
        json={**NEW_USER, "dni": "41234568", "username": "otro", "email": "otro@correo.test", "phone": ""},
    )
    assert response.status_code == 201 and response.json()["user"]["phone"] is None


async def test_get_user_detail(
    admin_client: httpx.AsyncClient, admin_user: BoUser, operator_user: BoUser, session_factory: Sessions
) -> None:
    await update_user(session_factory, operator_user.id, created_by=admin_user.id)
    detail = await detail_of(admin_client, operator_user.id)
    assert detail["id"] == str(operator_user.id)
    assert detail["createdBy"] == admin_ref(admin_user)
    # Creado por la CLI (o sin autor): nulo.
    assert detail["updatedBy"] is None
    assert detail["version"] == 1


async def test_get_user_not_found(
    admin_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    response = await admin_client.get(f"{USERS}/{uuid.uuid4()}")
    assert response.status_code == 404 and response.json() == NOT_FOUND

    await update_user(session_factory, operator_user.id, is_deleted=1)
    response = await admin_client.get(f"{USERS}/{operator_user.id}")
    assert response.status_code == 404 and response.json() == NOT_FOUND

    response = await admin_client.get(f"{USERS}/no-es-un-uuid")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "userId"]
    assert response.json()["detail"][0]["msg"] == "Debe ser un UUID válido"


async def test_update_user(
    admin_client: httpx.AsyncClient, admin_user: BoUser, operator_user: BoUser, session_factory: Sessions
) -> None:
    before = await detail_of(admin_client, operator_user.id)
    response = await admin_client.put(
        f"{USERS}/{operator_user.id}",
        json=put_body(
            before, firstNames="  María   Elena ", email="MQUISPE.NUEVO@correo.test", roleCode="ADMIN"
        ),
    )

    assert response.status_code == 200, response.text
    after = response.json()
    assert after["firstNames"] == "María Elena"
    assert after["email"] == "mquispe.nuevo@correo.test"
    assert after["role"] == {"code": "ADMIN", "name": "Administrador"}
    assert after["version"] == 2
    assert after["updatedBy"] == admin_ref(admin_user)
    assert after["username"] == before["username"]

    (entry,) = await audit_rows(session_factory, AuditAction.USER_UPDATED)
    assert entry.actor_user_id == admin_user.id and entry.entity_id == str(operator_user.id)
    # Solo los campos que cambiaron.
    assert entry.details == {
        "changes": {
            "firstNames": {"from": "María José", "to": "María Elena"},
            "email": {"from": "mquispe.qa@correo.test", "to": "mquispe.nuevo@correo.test"},
            "roleCode": {"from": "OPERADOR", "to": "ADMIN"},
        }
    }


async def test_update_without_changes_keeps_version_and_audit(
    admin_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    before = await detail_of(admin_client, operator_user.id)
    stored_before = await get_user(session_factory, operator_user.id)
    # Mismos valores (normalizados igual que los guardados).
    response = await admin_client.put(
        f"{USERS}/{operator_user.id}", json=put_body(before, email=before["email"].upper())
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1
    stored = await get_user(session_factory, operator_user.id)
    assert stored.updated_at == stored_before.updated_at and stored.updated_by is None
    assert await audit_rows(session_factory, AuditAction.USER_UPDATED) == []


async def test_update_without_phone_removes_it(
    admin_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    before = await detail_of(admin_client, operator_user.id)
    body = put_body(before)
    del body["phone"]
    response = await admin_client.put(f"{USERS}/{operator_user.id}", json=body)
    assert response.status_code == 200 and response.json()["phone"] is None

    (entry,) = await audit_rows(session_factory, AuditAction.USER_UPDATED)
    assert entry.details == {"changes": {"phone": {"from": "987654321", "to": None}}}

    after = response.json()
    response = await admin_client.put(f"{USERS}/{operator_user.id}", json=put_body(after, phone="912345678"))
    assert response.status_code == 200 and response.json()["phone"] == "912345678"


async def test_username_is_not_editable(admin_client: httpx.AsyncClient, operator_user: BoUser) -> None:
    before = await detail_of(admin_client, operator_user.id)
    response = await admin_client.put(
        f"{USERS}/{operator_user.id}", json=put_body(before, username="otro.nombre")
    )
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "extra_forbidden", "loc": ["body", "username"], "msg": "Campo no permitido"}
    ]


async def test_delete_user(
    admin_client: httpx.AsyncClient, admin_user: BoUser, operator_user: BoUser, session_factory: Sessions
) -> None:
    response = await admin_client.delete(f"{USERS}/{operator_user.id}")

    assert response.status_code == 204 and response.content == b""
    stored = await get_user(session_factory, operator_user.id)
    assert stored.is_deleted == 1 and stored.deleted_at is not None
    assert stored.deleted_by == admin_user.id and stored.updated_by == admin_user.id
    assert (await admin_client.get(f"{USERS}/{operator_user.id}")).status_code == 404
    listed = (await admin_client.get(USERS)).json()
    assert [item["username"] for item in listed["items"]] == [admin_user.username]

    (entry,) = await audit_rows(session_factory, AuditAction.USER_DELETED)
    assert entry.actor_user_id == admin_user.id and entry.entity_id == str(operator_user.id)
    assert entry.details == {"username": "mquispe.qa"}

    # Un usuario eliminado ya no admite acciones.
    for path in ("", "/activate", "/deactivate", "/unlock", "/reset-password"):
        method = "DELETE" if path == "" else "POST"
        response = await admin_client.request(method, f"{USERS}/{operator_user.id}{path}")
        assert response.status_code == 404 and response.json() == NOT_FOUND, path


async def test_deleted_user_frees_its_values(admin_client: httpx.AsyncClient, operator_user: BoUser) -> None:
    assert (await admin_client.delete(f"{USERS}/{operator_user.id}")).status_code == 204
    response = await admin_client.post(
        USERS,
        json={
            **NEW_USER,
            "dni": operator_user.dni,
            "username": operator_user.username,
            "email": operator_user.email,
        },
    )
    assert response.status_code == 201, response.text


async def test_activate_and_deactivate_are_idempotent(
    admin_client: httpx.AsyncClient, admin_user: BoUser, operator_user: BoUser, session_factory: Sessions
) -> None:
    response = await admin_client.post(f"{USERS}/{operator_user.id}/deactivate")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "INACTIVO"
    assert response.json()["updatedBy"] == admin_ref(admin_user)
    assert response.json()["version"] == 1  # las acciones no suben la versión
    assert (await admin_client.post(f"{USERS}/{operator_user.id}/deactivate")).status_code == 200

    response = await admin_client.post(f"{USERS}/{operator_user.id}/activate")
    assert response.status_code == 200 and response.json()["status"] == "ACTIVO"
    assert (await admin_client.post(f"{USERS}/{operator_user.id}/activate")).status_code == 200

    # Bitácora solo cuando hubo cambio.
    assert len(await audit_rows(session_factory, AuditAction.USER_DEACTIVATED)) == 1
    (activated,) = await audit_rows(session_factory, AuditAction.USER_ACTIVATED)
    assert activated.actor_user_id == admin_user.id and activated.entity_id == str(operator_user.id)
    assert activated.details is None


async def test_activate_does_not_touch_authorship_when_nothing_changes(
    admin_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    before = await get_user(session_factory, operator_user.id)
    assert (await admin_client.post(f"{USERS}/{operator_user.id}/activate")).status_code == 200
    assert (await admin_client.post(f"{USERS}/{operator_user.id}/unlock")).status_code == 200
    after = await get_user(session_factory, operator_user.id)
    assert after.updated_at == before.updated_at and after.updated_by is None
    assert await audit_rows(session_factory, AuditAction.USER_ACTIVATED) == []
    assert await audit_rows(session_factory, AuditAction.USER_UNLOCKED) == []


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"locked_until": datetime.now(UTC) + timedelta(minutes=10)}, id="bloqueado"),
        pytest.param({"failed_login_attempts": 3}, id="con-intentos"),
        pytest.param({"locked_until": datetime.now(UTC) - timedelta(minutes=1)}, id="bloqueo-vencido"),
    ],
)
async def test_unlock(
    admin_client: httpx.AsyncClient,
    make_bo_client: ClientFactory,
    admin_user: BoUser,
    operator_user: BoUser,
    session_factory: Sessions,
    values: dict[str, Any],
) -> None:
    await update_user(session_factory, operator_user.id, **values)

    response = await admin_client.post(f"{USERS}/{operator_user.id}/unlock")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ACTIVO" and response.json()["lockedUntil"] is None
    stored = await get_user(session_factory, operator_user.id)
    assert stored.failed_login_attempts == 0 and stored.locked_until is None
    (entry,) = await audit_rows(session_factory, AuditAction.USER_UNLOCKED)
    assert entry.actor_user_id == admin_user.id and entry.entity_id == str(operator_user.id)
    # Repetido: sin cambio, sin bitácora.
    assert (await admin_client.post(f"{USERS}/{operator_user.id}/unlock")).status_code == 200
    assert len(await audit_rows(session_factory, AuditAction.USER_UNLOCKED)) == 1
    assert (await login_as(make_bo_client(), operator_user)).status_code == 200


async def test_reset_password_by_admin(
    admin_client: httpx.AsyncClient,
    make_bo_client: ClientFactory,
    admin_user: BoUser,
    operator_user: BoUser,
    session_factory: Sessions,
) -> None:
    await update_user(
        session_factory,
        operator_user.id,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
        failed_login_attempts=2,
    )

    response = await admin_client.post(f"{USERS}/{operator_user.id}/reset-password")

    assert response.status_code == 200, response.text
    temporary = response.json()["temporaryPassword"]
    assert set(response.json()) == {"temporaryPassword"} and len(temporary) == 20
    stored = await get_user(session_factory, operator_user.id)
    assert stored.must_change_password is True
    assert stored.locked_until is None and stored.failed_login_attempts == 0
    assert stored.updated_by == admin_user.id and stored.version == 1

    (entry,) = await audit_rows(session_factory, AuditAction.USER_PASSWORD_RESET_BY_ADMIN)
    assert entry.actor_user_id == admin_user.id and entry.details == {"source": "API"}

    assert (await login_as(make_bo_client(), operator_user)).status_code == 401
    login = await login_as(make_bo_client(), operator_user, password=temporary)
    assert login.status_code == 200 and login.json()["mustChangePassword"] is True


async def test_reset_password_of_inactive_user_is_allowed(
    admin_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    await update_user(session_factory, operator_user.id, is_active=False)
    response = await admin_client.post(f"{USERS}/{operator_user.id}/reset-password")
    assert response.status_code == 200


# --- B4.2 Validaciones ---


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dni", "1234567", "El DNI debe tener 8 dígitos."),
        ("dni", "1234567a", "El DNI debe tener 8 dígitos."),
        ("firstNames", "Juan3", "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion."),
        ("lastNames", "   ", "No puede estar vacío"),
        ("lastNames", "a" * 101, "Debe tener como máximo 100 caracteres"),
        (
            "username",
            "1rosa",
            USERNAME_INVALID,
        ),
        (
            "username",
            "ros",
            USERNAME_INVALID,
        ),
        ("email", "no-es-un-correo", "El correo no tiene un formato válido."),
        ("phone", "98711122", "El celular debe tener 9 dígitos."),
        ("roleCode", "SUPERVISOR", "Debe ser uno de estos valores: 'ADMIN' o 'OPERADOR'"),
        ("isActive", True, "Campo no permitido"),
        ("dni", None, "Debe ser un texto"),
    ],
)
async def test_create_user_validation(
    admin_client: httpx.AsyncClient, session_factory: Sessions, field: str, value: Any, message: str
) -> None:
    response = await admin_client.post(USERS, json={**NEW_USER, field: value})
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", field] and error["msg"] == message
    assert "input" not in error
    assert await audit_rows(session_factory, AuditAction.USER_CREATED) == []


@pytest.mark.parametrize("field", ["dni", "firstNames", "lastNames", "username", "email", "roleCode"])
async def test_create_user_required_fields(admin_client: httpx.AsyncClient, field: str) -> None:
    body = {key: value for key, value in NEW_USER.items() if key != field}
    response = await admin_client.post(USERS, json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "missing", "loc": ["body", field], "msg": "Campo obligatorio"}
    ]


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"version": 0}, "version", "Debe ser mayor o igual que 1"),
        ({"version": None}, "version", "Debe ser un número entero"),
        ({"dni": "123"}, "dni", "El DNI debe tener 8 dígitos."),
        ({"email": "sin-arroba"}, "email", "El correo no tiene un formato válido."),
        ({"phone": "12"}, "phone", "El celular debe tener 9 dígitos."),
        ({"roleCode": "admin"}, "roleCode", "Debe ser uno de estos valores: 'ADMIN' o 'OPERADOR'"),
    ],
)
async def test_update_user_validation(
    admin_client: httpx.AsyncClient, operator_user: BoUser, changes: dict[str, Any], field: str, message: str
) -> None:
    before = await detail_of(admin_client, operator_user.id)
    response = await admin_client.put(f"{USERS}/{operator_user.id}", json=put_body(before, **changes))
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", field] and error["msg"] == message


# --- B4.3 Duplicados ---


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"dni": "45871236"}, "dni", "Ya existe un usuario con ese DNI."),
        ({"username": "JPEREZ.QA"}, "username", "Ya existe un usuario con ese nombre de usuario."),
        ({"email": "JPerez.QA@correo.test"}, "email", "Ya existe un usuario con ese correo."),
    ],
)
async def test_create_user_duplicates(
    admin_client: httpx.AsyncClient, changes: dict[str, str], field: str, message: str
) -> None:
    response = await admin_client.post(USERS, json={**NEW_USER, **changes})
    assert response.status_code == 409
    assert response.json() == {"detail": message, "code": "DUPLICATE", "field": field}


@pytest.mark.parametrize(
    ("changes", "field"),
    [({"dni": "45871236"}, "dni"), ({"email": "jperez.qa@correo.test"}, "email")],
)
async def test_update_user_duplicates(
    admin_client: httpx.AsyncClient,
    operator_user: BoUser,
    session_factory: Sessions,
    changes: dict[str, str],
    field: str,
) -> None:
    before = await detail_of(admin_client, operator_user.id)
    response = await admin_client.put(f"{USERS}/{operator_user.id}", json=put_body(before, **changes))
    assert response.status_code == 409
    assert response.json()["code"] == "DUPLICATE" and response.json()["field"] == field
    assert (await get_user(session_factory, operator_user.id)).version == 1


async def test_concurrent_creations_only_one_wins(
    make_bo_client: ClientFactory, admin_user: BoUser, session_factory: Sessions
) -> None:
    first = await logged_client(make_bo_client, admin_user)
    second = await logged_client(make_bo_client, admin_user)
    # Mismo correo, distinto DNI y usuario: el que pierde responde 409 en el correo (por el chequeo o por el índice).
    responses = await asyncio.gather(
        first.post(USERS, json=NEW_USER),
        second.post(USERS, json={**NEW_USER, "dni": "41234568", "username": "rhuaman2"}),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    (conflict,) = [response for response in responses if response.status_code == 409]
    assert conflict.json() == {
        "detail": "Ya existe un usuario con ese correo.",
        "code": "DUPLICATE",
        "field": "email",
    }
    assert len(await audit_rows(session_factory, AuditAction.USER_CREATED)) == 1


async def test_duplicate_from_unique_index_is_translated(
    session_factory: Sessions, admin_user: BoUser, test_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La carrera llega al índice único parcial: el IntegrityError se traduce al mismo 409."""

    async def no_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(users_service, "ensure_unique_user_fields", no_check)
    from app.schemas.backoffice.users import UserCreateRequest

    data = UserCreateRequest.model_validate({**NEW_USER, "dni": admin_user.dni})
    async with session_factory() as db:
        with pytest.raises(BackofficeError) as raised:
            await users_service.create_user(db, data, settings=test_settings, context=AuditContext())
    assert raised.value.status_code == 409 and raised.value.field == "dni"


# --- B4.4 Concurrencia optimista ---


async def test_version_conflict(
    admin_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    stale = await detail_of(admin_client, operator_user.id)
    updated = await admin_client.put(
        f"{USERS}/{operator_user.id}", json=put_body(stale, firstNames="Primera")
    )
    assert updated.json()["version"] == 2

    response = await admin_client.put(
        f"{USERS}/{operator_user.id}", json=put_body(stale, firstNames="Segunda")
    )

    assert response.status_code == 409 and response.json() == VERSION_CONFLICT
    stored = await get_user(session_factory, operator_user.id)
    assert stored.first_names == "Primera" and stored.version == 2
    # Una versión futura tampoco sirve.
    response = await admin_client.put(f"{USERS}/{operator_user.id}", json=put_body(stale, version=9))
    assert response.status_code == 409


async def test_login_and_own_password_changes_do_not_touch_authorship(
    make_bo_client: ClientFactory, operator_user: BoUser, session_factory: Sessions
) -> None:
    """Decisión de la sesión 3: el login, los intentos fallidos, el bloqueo y el cambio de contraseña propio no
    cambian `version`, `updated_at` ni `updated_by`."""
    before = await get_user(session_factory, operator_user.id)
    client = make_bo_client()
    for _ in range(5):
        await login_as(client, operator_user, password="Mala#Clave1")
    await update_user(session_factory, operator_user.id, locked_until=None)
    # update_user (helper) no toca updated_at: no tiene onupdate.
    assert (await login_as(client, operator_user)).status_code == 200
    response = await client.post(
        f"{BO}/auth/change-password",
        json={"currentPassword": operator_user.password, "newPassword": "Nueva#Clave9"},
    )
    assert response.status_code == 204

    after = await get_user(session_factory, operator_user.id)
    assert after.last_login_at is not None and after.password_changed_at is not None
    assert (after.version, after.updated_at, after.updated_by) == (1, before.updated_at, None)


# --- B4.5 Acciones sobre sí mismo ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/deactivate"),
        ("DELETE", ""),
        ("POST", "/reset-password"),
    ],
)
async def test_self_actions_not_allowed(
    admin_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    method: str,
    path: str,
) -> None:
    # Hay otro Administrador activo: el error es por actuar sobre sí mismo, no por ser el último.
    await make_user(session_factory, 1, role_code=RoleCode.ADMIN)
    response = await admin_client.request(method, f"{USERS}/{admin_user.id}{path}")
    assert response.status_code == 409 and response.json() == SELF_ACTION
    stored = await get_user(session_factory, admin_user.id)
    assert stored.is_active and stored.is_deleted == 0 and not stored.must_change_password


async def test_cannot_change_own_role_but_can_edit_own_data(
    admin_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await make_user(session_factory, 1, role_code=RoleCode.ADMIN)
    before = await detail_of(admin_client, admin_user.id)

    response = await admin_client.put(f"{USERS}/{admin_user.id}", json=put_body(before, roleCode="OPERADOR"))
    assert response.status_code == 409 and response.json() == SELF_ACTION

    response = await admin_client.put(f"{USERS}/{admin_user.id}", json=put_body(before, phone="912345678"))
    assert response.status_code == 200 and response.json()["phone"] == "912345678"
    assert response.json()["updatedBy"] == admin_ref(admin_user)


# --- B4.6 Último Administrador ---


@pytest.mark.parametrize("action", ["deactivate", "delete", "role"])
async def test_last_admin_rule(session_factory: Sessions, operator_user: BoUser, action: str) -> None:
    """Por la API la regla solo salta en una carrera (el actor siempre es otro Administrador activo y las acciones
    sobre sí mismo ya responden SELF_ACTION_NOT_ALLOWED): aquí se llama al servicio con un actor que no es
    Administrador y un único Administrador activo."""
    only_admin = await make_user(session_factory, 1, role_code=RoleCode.ADMIN)
    # Un Administrador inactivo o eliminado no cuenta.
    await make_user(session_factory, 2, role_code=RoleCode.ADMIN, is_active=False)
    await make_user(session_factory, 3, role_code=RoleCode.ADMIN, is_deleted=1)
    context = AuditContext()

    async with session_factory() as db:
        with pytest.raises(BackofficeError) as raised:
            if action == "deactivate":
                await users_service.deactivate_user(
                    db, only_admin.id, actor_user_id=operator_user.id, context=context
                )
            elif action == "delete":
                await users_service.delete_user(
                    db, only_admin.id, actor_user_id=operator_user.id, context=context
                )
            else:
                data = UserUpdateRequest(
                    dni=only_admin.dni,
                    first_names=only_admin.first_names,
                    last_names=only_admin.last_names,
                    email=only_admin.email,
                    phone=None,
                    role_code=RoleCode.OPERADOR,
                    version=1,
                )
                await users_service.update_user(
                    db, only_admin.id, data, actor_user_id=operator_user.id, context=context
                )
    assert (raised.value.status_code, raised.value.code.value) == (409, "LAST_ADMIN")
    assert raised.value.detail == LAST_ADMIN["detail"]
    stored = await get_user(session_factory, only_admin.id)
    assert stored.is_active and stored.is_deleted == 0 and stored.role_code == "ADMIN"
    assert await audit_rows(session_factory) == []


async def test_last_admin_rule_only_applies_to_admins(session_factory: Sessions, admin_user: BoUser) -> None:
    operator = await make_user(session_factory, 1)
    async with session_factory() as db:
        await users_service.deactivate_user(
            db, operator.id, actor_user_id=admin_user.id, context=AuditContext()
        )
    assert not (await get_user(session_factory, operator.id)).is_active


async def test_removing_admin_role_with_another_admin_is_allowed(
    admin_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    other = await make_user(session_factory, 1, role_code=RoleCode.ADMIN)
    before = await detail_of(admin_client, other.id)
    response = await admin_client.put(f"{USERS}/{other.id}", json=put_body(before, roleCode="OPERADOR"))
    assert response.status_code == 200 and response.json()["role"]["code"] == "OPERADOR"


@pytest.mark.parametrize("action", ["deactivate", "delete"])
async def test_admins_acting_on_each_other_in_parallel(
    make_bo_client: ClientFactory, admin_user: BoUser, session_factory: Sessions, action: str
) -> None:
    other = await make_user(session_factory, 1, role_code=RoleCode.ADMIN)
    first = await logged_client(make_bo_client, admin_user)
    second = await logged_client(make_bo_client, other)

    async def act(client: httpx.AsyncClient, target: BoUser) -> httpx.Response:
        if action == "delete":
            return await client.delete(f"{USERS}/{target.id}")
        return await client.post(f"{USERS}/{target.id}/deactivate")

    responses = await asyncio.gather(act(first, other), act(second, admin_user))

    success = 204 if action == "delete" else 200
    statuses = [response.status_code for response in responses]
    # Solo uno lo logra. El que pierde recibe LAST_ADMIN o, si el otro ya confirmó, 401 (su sesión ya fue revocada).
    assert statuses.count(success) == 1, [response.text for response in responses]
    (loser,) = [response for response in responses if response.status_code != success]
    assert loser.json() == LAST_ADMIN or loser.json()["code"] == "SESSION_EXPIRED"
    remaining = [
        user
        for user in (
            await get_user(session_factory, admin_user.id),
            await get_user(session_factory, other.id),
        )
        if user.is_active and user.is_deleted == 0
    ]
    assert len(remaining) == 1


# --- B4.7 Las acciones revocan las sesiones del afectado ---


@pytest.mark.parametrize(
    ("method", "path", "reason"),
    [
        ("POST", "/deactivate", "USER_DEACTIVATED"),
        ("DELETE", "", "USER_DELETED"),
        ("POST", "/reset-password", "ADMIN_PASSWORD_RESET"),
    ],
)
async def test_actions_revoke_sessions_of_the_user(
    admin_client: httpx.AsyncClient,
    make_bo_client: ClientFactory,
    operator_user: BoUser,
    session_factory: Sessions,
    method: str,
    path: str,
    reason: str,
) -> None:
    target = await logged_client(make_bo_client, operator_user)
    other_device = await logged_client(make_bo_client, operator_user)
    assert (await target.get(f"{BO}/auth/me")).status_code == 200

    response = await admin_client.request(method, f"{USERS}/{operator_user.id}{path}")
    assert response.status_code in (200, 204), response.text

    for client in (target, other_device):
        response = await client.get(f"{BO}/auth/me")
        assert response.status_code == 401 and response.json()["code"] == "SESSION_EXPIRED"
    sessions = await user_sessions(session_factory, operator_user.id)
    assert len(sessions) == 2
    assert all(row.revoked_at is not None and row.revoked_reason == reason for row in sessions)


async def test_reset_password_invalidates_pending_reset_links(
    admin_client: httpx.AsyncClient,
    bo_client: httpx.AsyncClient,
    operator_user: BoUser,
    session_factory: Sessions,
) -> None:
    await bo_client.post(f"{BO}/auth/forgot-password", json={"identifier": operator_user.username})
    assert (await admin_client.post(f"{USERS}/{operator_user.id}/reset-password")).status_code == 200
    (token,) = await reset_tokens(session_factory, operator_user.id)
    assert token.invalidated_at is not None


# --- B4.8 Lista ---


@pytest.fixture
async def listed_users(
    session_factory: Sessions, admin_user: BoUser, operator_user: BoUser
) -> dict[str, BoUser]:
    """admin_user (jperez.qa, ADMIN), operator_user (mquispe.qa) y 5 más con estados distintos."""
    now = datetime.now(UTC)
    return {
        "admin": admin_user,
        "operator": operator_user,
        "inactive": await make_user(
            session_factory, 1, username="lrojas", first_names="Luis", last_names="Rojas", is_active=False
        ),
        "locked": await make_user(
            session_factory,
            2,
            username="ana_b",
            first_names="Ana",
            last_names="Benítez",
            locked_until=now + timedelta(minutes=10),
            last_login_at=now - timedelta(days=1),
        ),
        "expired_lock": await make_user(
            session_factory,
            3,
            username="anaxb",
            first_names="Anabel",
            last_names="Xu",
            email="anaxb%100@correo.test",
            locked_until=now - timedelta(minutes=1),
            last_login_at=now - timedelta(days=2),
        ),
        "deleted": await make_user(
            session_factory, 4, username="borrado", first_names="Ana", last_names="Borrada", is_deleted=1
        ),
        "admin2": await make_user(
            session_factory,
            5,
            username="zadmin",
            first_names="Zoila",
            last_names="Zapata",
            role_code=RoleCode.ADMIN,
        ),
    }


async def usernames(client: httpx.AsyncClient, **params: Any) -> list[str]:
    response = await client.get(USERS, params=params)
    assert response.status_code == 200, response.text
    return [item["username"] for item in response.json()["items"]]


async def test_list_users_default_order_and_shape(
    admin_client: httpx.AsyncClient, listed_users: dict[str, BoUser]
) -> None:
    response = await admin_client.get(USERS)
    body = response.json()
    assert (body["total"], body["page"], body["pageSize"]) == (6, 1, 20)
    # Apellidos y luego nombres; sin el eliminado.
    assert [item["username"] for item in body["items"]] == [
        "ana_b",
        "jperez.qa",
        "mquispe.qa",
        "lrojas",
        "anaxb",
        "zadmin",
    ]
    item = body["items"][0]
    assert set(item) == {
        "id",
        "username",
        "firstNames",
        "lastNames",
        "dni",
        "email",
        "role",
        "status",
        "lastLoginAt",
    }
    assert item["role"] == {"code": "OPERADOR", "name": "Operador"} and ISO_LIMA.match(item["lastLoginAt"])


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("50000001", ["lrojas"]),
        ("JPEREZ", ["jperez.qa"]),
        ("pérez gómez", ["jperez.qa"]),
        ("maría josé quispe", ["mquispe.qa"]),
        ("  rojas  ", ["lrojas"]),
        ("@correo.test", ["ana_b", "jperez.qa", "mquispe.qa", "lrojas", "anaxb", "zadmin"]),
        # Los comodines se buscan literales: "_" no es "cualquier carácter" ni "%" es "cualquier texto".
        ("a_b", ["ana_b"]),
        ("%100", ["anaxb"]),
        ("%", ["anaxb"]),
        ("borrada", []),
    ],
)
async def test_list_users_search(
    admin_client: httpx.AsyncClient, listed_users: dict[str, BoUser], q: str, expected: list[str]
) -> None:
    assert await usernames(admin_client, q=q) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"roleCode": "ADMIN"}, ["jperez.qa", "zadmin"]),
        ({"status": "INACTIVO"}, ["lrojas"]),
        ({"status": "BLOQUEADO"}, ["ana_b"]),
        ({"status": "ACTIVO"}, ["jperez.qa", "mquispe.qa", "anaxb", "zadmin"]),
        ({"status": "ACTIVO", "roleCode": "OPERADOR", "q": "ana"}, ["anaxb"]),
        ({"sortBy": "username"}, ["ana_b", "anaxb", "jperez.qa", "lrojas", "mquispe.qa", "zadmin"]),
        (
            {"sortBy": "username", "sortOrder": "desc"},
            ["zadmin", "mquispe.qa", "lrojas", "jperez.qa", "anaxb", "ana_b"],
        ),
        ({"sortBy": "lastNames", "sortOrder": "desc", "pageSize": 10}, ["zadmin", "anaxb", "lrojas"]),
    ],
)
async def test_list_users_filters_and_sorting(
    admin_client: httpx.AsyncClient,
    listed_users: dict[str, BoUser],
    params: dict[str, Any],
    expected: list[str],
) -> None:
    result = await usernames(admin_client, **params)
    assert result[: len(expected)] == expected


async def test_list_users_sort_by_last_login_keeps_nulls_last(
    admin_client: httpx.AsyncClient, listed_users: dict[str, BoUser]
) -> None:
    ascending = await usernames(admin_client, sortBy="lastLoginAt")
    descending = await usernames(admin_client, sortBy="lastLoginAt", sortOrder="desc")
    # admin_user (jperez.qa) inició sesión con admin_client: es el acceso más reciente.
    assert ascending[:3] == ["anaxb", "ana_b", "jperez.qa"]
    assert descending[:3] == ["jperez.qa", "ana_b", "anaxb"]
    # Sin último acceso: al final en ambos sentidos.
    never = {"mquispe.qa", "lrojas", "zadmin"}
    assert set(ascending[3:]) == never and set(descending[3:]) == never


async def test_list_users_sort_by_created_at(
    admin_client: httpx.AsyncClient, listed_users: dict[str, BoUser], session_factory: Sessions
) -> None:
    base = datetime.now(UTC) - timedelta(days=10)
    order = ["zadmin", "lrojas", "mquispe.qa", "jperez.qa", "anaxb", "ana_b"]
    for position, key in enumerate(["admin2", "inactive", "operator", "admin", "expired_lock", "locked"]):
        await update_user(session_factory, listed_users[key].id, created_at=base + timedelta(hours=position))
    assert await usernames(admin_client, sortBy="createdAt") == order
    assert await usernames(admin_client, sortBy="createdAt", sortOrder="desc") == order[::-1]


async def test_list_users_pagination(admin_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    for number in range(1, 12):
        await make_user(session_factory, number, last_names=f"Apellido {number:02d}")
    page_one = (await admin_client.get(USERS, params={"pageSize": 10})).json()
    page_two = (await admin_client.get(USERS, params={"pageSize": 10, "page": 2})).json()
    page_three = (await admin_client.get(USERS, params={"pageSize": 10, "page": 3})).json()

    assert (page_one["total"], len(page_one["items"]), page_one["pageSize"]) == (12, 10, 10)
    assert (page_two["page"], len(page_two["items"])) == (2, 2)
    assert page_three["items"] == [] and page_three["total"] == 12
    seen = [item["id"] for item in page_one["items"] + page_two["items"]]
    assert len(set(seen)) == 12


@pytest.mark.parametrize(
    ("params", "field", "message"),
    [
        ({"pageSize": 30}, "pageSize", "Debe ser uno de estos valores: 10, 20, 50 o 100"),
        ({"page": 0}, "page", "Debe ser mayor o igual que 1"),
        ({"q": "a" * 101}, "q", "Debe tener como máximo 100 caracteres"),
        (
            {"status": "SUSPENDIDO"},
            "status",
            "Debe ser uno de estos valores: 'ACTIVO', 'INACTIVO' o 'BLOQUEADO'",
        ),
        ({"roleCode": "ROOT"}, "roleCode", "Debe ser uno de estos valores: 'ADMIN' o 'OPERADOR'"),
        ({"sortBy": "dni"}, "sortBy", None),
        ({"sortOrder": "up"}, "sortOrder", "Debe ser uno de estos valores: 'asc' o 'desc'"),
    ],
)
async def test_list_users_query_validation(
    admin_client: httpx.AsyncClient, params: dict[str, Any], field: str, message: str | None
) -> None:
    response = await admin_client.get(USERS, params=params)
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["query", field]
    if message is not None:
        assert error["msg"] == message


# --- Nada sensible en los logs ---


async def test_users_logs_have_no_personal_data(
    admin_client: httpx.AsyncClient, operator_user: BoUser, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        created = await admin_client.post(USERS, json=NEW_USER)
        user_id = created.json()["user"]["id"]
        detail = await detail_of(admin_client, user_id)
        await admin_client.put(f"{USERS}/{user_id}", json=put_body(detail, email="nuevo.correo@correo.test"))
        await admin_client.post(USERS, json=NEW_USER | {"username": "otro"})  # 409 por DNI
        await admin_client.post(USERS, json=NEW_USER | {"dni": "12"})  # 422
        reset = await admin_client.post(f"{USERS}/{user_id}/reset-password")
        await admin_client.post(f"{USERS}/{user_id}/deactivate")
        await admin_client.get(USERS, params={"q": "41234567"})
        await admin_client.delete(f"{USERS}/{user_id}")

    records = [record for record in caplog.records if record.name != "httpx"]
    assert records
    text = "\n".join(record.getMessage() for record in records)
    text += json.dumps([vars(record) for record in records], default=str)
    for secret in (
        "41234567",
        "Rosa",
        "Huamán",
        "rhuaman",
        "@correo.test",
        "987111222",
        created.json()["temporaryPassword"],
        reset.json()["temporaryPassword"],
        "$argon2id$",
    ):
        assert secret not in text, secret
