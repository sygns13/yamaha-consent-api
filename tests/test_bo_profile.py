"""Mi perfil del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, sección 6.4, fase B4): criterio B4.10.

Decisiones del responsable (sesión 3): la edición del perfil sube `version` y fija `updated_by` = el propio usuario;
sin cambios reales no hay bitácora ni cambio de versión.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.models import AuditAction, RoleCode
from tests.bo_helpers import (
    BO,
    BoUser,
    Sessions,
    audit_rows,
    create_bo_user,
    get_user,
    logged_client,
)

PROFILE = f"{BO}/profile"
ClientFactory = Callable[[], httpx.AsyncClient]
CURRENT_PASSWORD_INVALID = {
    "detail": "La contraseña actual no es correcta.",
    "code": "CURRENT_PASSWORD_INVALID",
    "field": "currentPassword",
}


@pytest.fixture
async def operator_client(make_bo_client: ClientFactory, operator_user: BoUser) -> httpx.AsyncClient:
    return await logged_client(make_bo_client, operator_user)


async def test_get_profile_is_me(operator_client: httpx.AsyncClient) -> None:
    profile = await operator_client.get(PROFILE)
    me = await operator_client.get(f"{BO}/auth/me")
    assert profile.status_code == 200
    assert profile.json() == me.json()
    assert profile.json()["modules"] == [{"code": "DEALERS", "name": "Gestión de Dealers"}]


async def test_profile_requires_a_session(bo_client: httpx.AsyncClient) -> None:
    for method in ("GET", "PATCH"):
        response = await bo_client.request(method, PROFILE, json={} if method == "PATCH" else None)
        assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"


async def test_profile_with_pending_password_change(
    make_bo_client: ClientFactory, session_factory: Sessions
) -> None:
    user = await create_bo_user(
        session_factory,
        username="nuevo.qa",
        dni="40000001",
        email="nuevo.qa@correo.test",
        must_change_password=True,
    )
    client = await logged_client(make_bo_client, user)
    for method in ("GET", "PATCH"):
        response = await client.request(method, PROFILE, json={"phone": None} if method == "PATCH" else None)
        assert response.status_code == 403 and response.json()["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_update_names_and_phone(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    response = await operator_client.patch(
        PROFILE, json={"firstNames": "  María   Elena ", "lastNames": "Quispe", "phone": "+51 912 345 678"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["firstNames"], body["lastNames"], body["phone"]) == ("María Elena", "Quispe", "912345678")
    assert body["email"] == operator_user.email and body["dni"] == operator_user.dni
    assert body["sessionExpiresAt"] == (await operator_client.get(f"{BO}/auth/me")).json()["sessionExpiresAt"]

    stored = await get_user(session_factory, operator_user.id)
    assert stored.version == 2 and stored.updated_by == operator_user.id
    (entry,) = await audit_rows(session_factory, AuditAction.PROFILE_UPDATED)
    assert entry.actor_user_id == operator_user.id
    assert entry.entity_type == "USER" and entry.entity_id == str(operator_user.id)
    assert entry.details == {
        "changes": {
            "firstNames": {"from": "María José", "to": "María Elena"},
            "lastNames": {"from": "Quispe Ñahui", "to": "Quispe"},
            "phone": {"from": "987654321", "to": "912345678"},
        }
    }


async def test_phone_null_removes_it_and_absent_keeps_it(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    response = await operator_client.patch(PROFILE, json={"firstNames": "Mariela"})
    assert response.json()["phone"] == "987654321"

    response = await operator_client.patch(PROFILE, json={"phone": None})
    assert response.status_code == 200 and response.json()["phone"] is None
    assert (await get_user(session_factory, operator_user.id)).phone is None


async def test_no_changes_no_audit(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    before = await get_user(session_factory, operator_user.id)
    for body in ({}, {"firstNames": "María José", "email": operator_user.email.upper()}):
        response = await operator_client.patch(PROFILE, json=body)
        assert response.status_code == 200, response.text
    stored = await get_user(session_factory, operator_user.id)
    assert (stored.version, stored.updated_at, stored.updated_by) == (1, before.updated_at, None)
    assert await audit_rows(session_factory, AuditAction.PROFILE_UPDATED) == []


async def test_changing_email_requires_current_password(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    new_email = {"email": "  Maria.Quispe@Correo.TEST "}

    response = await operator_client.patch(PROFILE, json=new_email)
    assert response.status_code == 422 and response.json() == CURRENT_PASSWORD_INVALID

    response = await operator_client.patch(PROFILE, json={**new_email, "currentPassword": "Mala#Clave1"})
    assert response.status_code == 422 and response.json() == CURRENT_PASSWORD_INVALID
    stored = await get_user(session_factory, operator_user.id)
    # No suma intentos de login ni cambia nada.
    assert stored.failed_login_attempts == 0 and stored.email == operator_user.email and stored.version == 1

    response = await operator_client.patch(
        PROFILE, json={**new_email, "currentPassword": operator_user.password}
    )
    assert response.status_code == 200, response.text
    assert response.json()["email"] == "maria.quispe@correo.test"
    (entry,) = await audit_rows(session_factory, AuditAction.PROFILE_UPDATED)
    assert entry.details == {
        "changes": {"email": {"from": operator_user.email, "to": "maria.quispe@correo.test"}}
    }
    # La contraseña nunca va a la bitácora.
    assert operator_user.password not in json.dumps(entry.details)


async def test_current_password_is_ignored_when_email_does_not_change(
    operator_client: httpx.AsyncClient,
) -> None:
    response = await operator_client.patch(
        PROFILE, json={"lastNames": "Ñahui", "currentPassword": "cualquiera"}
    )
    assert response.status_code == 200 and response.json()["lastNames"] == "Ñahui"


async def test_email_of_another_user_is_duplicate(
    operator_client: httpx.AsyncClient, operator_user: BoUser, admin_user: BoUser
) -> None:
    # Con la contraseña incorrecta no revela si el correo está en uso.
    response = await operator_client.patch(
        PROFILE, json={"email": admin_user.email, "currentPassword": "Mala#1x"}
    )
    assert response.status_code == 422 and response.json()["code"] == "CURRENT_PASSWORD_INVALID"

    response = await operator_client.patch(
        PROFILE, json={"email": admin_user.email.upper(), "currentPassword": operator_user.password}
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Ya existe un usuario con ese correo.",
        "code": "DUPLICATE",
        "field": "email",
    }


@pytest.mark.parametrize("field", ["dni", "username", "roleCode", "isActive", "version"])
async def test_cannot_change_dni_username_or_role(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions, field: str
) -> None:
    value: Any = {"dni": "11111111", "username": "otro.usuario", "roleCode": RoleCode.ADMIN.value}.get(
        field, 1
    )
    response = await operator_client.patch(PROFILE, json={field: value})
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "extra_forbidden", "loc": ["body", field], "msg": "Campo no permitido"}
    ]
    stored = await get_user(session_factory, operator_user.id)
    assert (stored.dni, stored.username, stored.role_code) == (
        operator_user.dni,
        operator_user.username,
        "OPERADOR",
    )


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"firstNames": None}, "firstNames", "Debe ser un texto"),
        ({"lastNames": "   "}, "lastNames", "No puede estar vacío"),
        (
            {"firstNames": "Juan<script>"},
            "firstNames",
            "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion.",
        ),
        ({"email": None}, "email", "Debe ser un texto"),
        ({"email": "sin-arroba", "currentPassword": "x"}, "email", "El correo no tiene un formato válido."),
        ({"phone": "12345"}, "phone", "El celular debe tener 9 dígitos."),
    ],
)
async def test_profile_validation(
    operator_client: httpx.AsyncClient, body: dict[str, Any], field: str, message: str
) -> None:
    response = await operator_client.patch(PROFILE, json=body)
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", field] and error["msg"] == message


async def test_admin_can_edit_own_profile(make_bo_client: ClientFactory, admin_user: BoUser) -> None:
    client = await logged_client(make_bo_client, admin_user)
    response = await client.patch(PROFILE, json={"phone": "911222333"})
    assert response.status_code == 200
    assert response.json()["phone"] == "911222333" and response.json()["role"]["code"] == "ADMIN"


async def test_profile_logs_have_no_personal_data(
    operator_client: httpx.AsyncClient, operator_user: BoUser, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await operator_client.patch(
            PROFILE, json={"email": "nuevo.correo@correo.test", "currentPassword": "Mal#1a"}
        )
        await operator_client.patch(
            PROFILE,
            json={
                "firstNames": "Mariela",
                "email": "nuevo.correo@correo.test",
                "phone": "912345678",
                "currentPassword": operator_user.password,
            },
        )
    records = [record for record in caplog.records if record.name != "httpx"]
    assert records
    text = json.dumps([vars(record) for record in records], default=str)
    for secret in (
        "nuevo.correo",
        "Mariela",
        "912345678",
        operator_user.password,
        "Mal#1a",
        operator_user.dni,
    ):
        assert secret not in text, secret
