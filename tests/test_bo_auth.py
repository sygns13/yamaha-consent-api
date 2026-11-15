"""Autenticación, sesiones y autorización del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, fase B2).

Criterios B2.1–B2.13 y B2.15. Los tiempos (bloqueo, inactividad, vencimiento) se simulan en la base, sin `sleep`.
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, FastAPI
from fastapi.openapi.utils import get_openapi

from app.api.v1 import backoffice
from app.core import passwords
from app.core.backoffice_security import (
    BackofficeError,
    CurrentSession,
    backoffice_error_handler,
    require_module,
)
from app.core.config import Settings, get_settings
from app.core.middleware import BackofficeHeadersMiddleware
from app.db.session import get_session
from app.models import AuditAction, ModuleCode, PasswordResetToken, RoleCode
from app.services.backoffice.sessions import AuthenticatedSession
from app.utils.tokens import hash_token
from tests.bo_helpers import (
    BO,
    DEFAULT_PASSWORD,
    BoUser,
    Sessions,
    audit_rows,
    create_bo_user,
    get_user,
    login_as,
    reset_tokens,
    session_cookie,
    update_sessions,
    update_user,
    user_sessions,
)

ISO_LIMA = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-05:00$")
INVALID_CREDENTIALS = {"detail": "Usuario o contraseña incorrectos.", "code": "INVALID_CREDENTIALS"}
SESSION_EXPIRED = {"detail": "Tu sesión expiró. Inicia sesión nuevamente.", "code": "SESSION_EXPIRED"}


def set_cookie_header(response: httpx.Response) -> str:
    (header,) = response.headers.get_list("set-cookie")
    return header


def cookie_was_cleared(response: httpx.Response) -> bool:
    header = set_cookie_header(response)
    return header.startswith('__session=""') and "Max-Age=0" in header and "Path=/api/v1/backoffice" in header


async def me(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get(f"{BO}/auth/me")


# --- B2.1 Login correcto ---


async def test_login_returns_me_and_session_cookie(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    response = await login_as(bo_client, admin_user)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "id": str(admin_user.id),
        "username": "jperez.qa",
        "firstNames": "Juan",
        "lastNames": "Pérez Gómez",
        "dni": "45871236",
        "email": "jperez.qa@correo.test",
        "phone": "987654321",
        "role": {"code": "ADMIN", "name": "Administrador"},
        "modules": [
            {"code": "DEALERS", "name": "Gestión de Dealers"},
            {"code": "USUARIOS", "name": "Administración de usuarios"},
            {"code": "AUDITORIA", "name": "Bitácora de auditoría"},
        ],
        "mustChangePassword": False,
        "lastLoginAt": body["lastLoginAt"],
        "sessionExpiresAt": body["sessionExpiresAt"],
    }
    assert ISO_LIMA.match(body["lastLoginAt"]) and ISO_LIMA.match(body["sessionExpiresAt"])
    assert datetime.fromisoformat(body["sessionExpiresAt"]) - datetime.fromisoformat(body["lastLoginAt"]) == (
        timedelta(hours=12)
    )

    header = set_cookie_header(response)
    token = session_cookie(bo_client)
    assert token and header.startswith(f"__session={token};")
    for attribute in ("HttpOnly", "SameSite=strict", "Path=/api/v1/backoffice", "Max-Age=43200"):
        assert attribute in header
    assert "Domain" not in header and "Secure" not in header  # test_settings: cookie no Secure (http://test)

    # La base guarda solo el hash del token.
    (stored,) = await user_sessions(session_factory, admin_user.id)
    assert stored.token_hash == hash_token(token) != token
    assert str(stored.ip_address) == "127.0.0.1" and stored.user_agent == "pytest-backoffice"
    assert stored.expires_at - stored.created_at == timedelta(hours=12)
    user = await get_user(session_factory, admin_user.id)
    assert user.last_login_at is not None
    (audit,) = await audit_rows(session_factory, AuditAction.AUTH_LOGIN_SUCCEEDED)
    assert (audit.actor_user_id, audit.entity_type, audit.entity_id) == (
        admin_user.id,
        "USER",
        str(admin_user.id),
    )
    assert str(audit.ip_address) == "127.0.0.1" and audit.request_id == response.headers["x-request-id"]


async def test_secure_cookie_by_default(
    bo_client: httpx.AsyncClient, admin_user: BoUser, test_settings: Settings
) -> None:
    test_settings.backoffice_session_cookie_secure = True
    response = await login_as(bo_client, admin_user)
    assert response.status_code == 200
    assert "Secure" in set_cookie_header(response)


async def test_operator_only_has_dealers(bo_client: httpx.AsyncClient, operator_user: BoUser) -> None:
    response = await login_as(bo_client, operator_user)
    assert response.json()["role"] == {"code": "OPERADOR", "name": "Operador"}
    assert response.json()["modules"] == [{"code": "DEALERS", "name": "Gestión de Dealers"}]
    assert (await me(bo_client)).json() == response.json()


async def test_username_is_trimmed_and_case_insensitive(
    bo_client: httpx.AsyncClient, admin_user: BoUser
) -> None:
    response = await bo_client.post(
        f"{BO}/auth/login", json={"username": "  JPerez.QA ", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"username": "jperez.qa"},
        {"username": "", "password": "x"},
        {"username": "jperez.qa", "password": ""},
        {"username": "a" * 31, "password": "x"},
        {"username": "jperez.qa", "password": "A" * 21},  # máximo de la política: 20 (v1.2)
        {"username": "jperez.qa", "password": "x", "remember": True},  # extra="forbid"
    ],
)
async def test_login_validation(bo_client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    response = await bo_client.post(f"{BO}/auth/login", json=body)
    assert response.status_code == 422
    assert all(set(item) == {"type", "loc", "msg"} for item in response.json()["detail"])


async def test_login_purges_old_sessions_of_the_user(
    make_bo_client: Callable[[], httpx.AsyncClient], admin_user: BoUser, session_factory: Sessions
) -> None:
    old, recent = make_bo_client(), make_bo_client()
    await login_as(old, admin_user)
    await old.post(f"{BO}/auth/logout")
    await update_sessions(session_factory, admin_user.id, revoked_at=datetime.now(UTC) - timedelta(days=31))
    await login_as(recent, admin_user)
    await recent.post(f"{BO}/auth/logout")  # revocada hoy: se conserva

    await login_as(make_bo_client(), admin_user)

    remaining = await user_sessions(session_factory, admin_user.id)
    assert len(remaining) == 2
    assert all(
        s.revoked_at is None or s.revoked_at > datetime.now(UTC) - timedelta(days=1) for s in remaining
    )


# --- B2.2 Credenciales inválidas ---


async def test_bad_password_and_unknown_user_get_the_same_401(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_calls: list[str] = []
    original = passwords.verify_dummy_password
    monkeypatch.setattr(passwords, "verify_dummy_password", lambda pw: dummy_calls.append(pw) or original(pw))

    bad = await login_as(bo_client, admin_user, password="Otra#Clave1")
    assert dummy_calls == []
    unknown = await bo_client.post(
        f"{BO}/auth/login", json={"username": "fantasma.qa", "password": "Otra#Clave1"}
    )
    assert dummy_calls == ["Otra#Clave1"]

    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json() == INVALID_CREDENTIALS
    assert "set-cookie" not in bad.headers and "set-cookie" not in unknown.headers

    failed = await audit_rows(session_factory, AuditAction.AUTH_LOGIN_FAILED)
    assert [(row.entity_id, row.details) for row in failed] == [
        (str(admin_user.id), {"reason": "BAD_PASSWORD"}),
        (None, {"reason": "UNKNOWN_USER"}),
    ]
    assert [row.actor_user_id for row in failed] == [admin_user.id, None]  # nulo solo si el usuario no existe
    assert all(row.entity_type == "USER" for row in failed)
    # La bitácora no guarda el usuario escrito.
    dumped = json.dumps([[str(v) for v in vars(row).values()] for row in failed])
    assert "fantasma" not in dumped
    assert (await get_user(session_factory, admin_user.id)).failed_login_attempts == 1


# --- B2.3 Bloqueo ---


async def test_lockout_after_max_failed_attempts(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    for _ in range(4):
        assert (await login_as(bo_client, admin_user, password="Mala#Clave1")).status_code == 401

    fifth = await login_as(bo_client, admin_user, password="Mala#Clave1")
    assert fifth.status_code == 423
    assert fifth.json() == {
        "detail": "Tu usuario está bloqueado temporalmente por intentos fallidos. Inténtalo nuevamente en 15 "
        "minutos o comunícate con el Administrador.",
        "code": "ACCOUNT_LOCKED",
    }
    user = await get_user(session_factory, admin_user.id)
    assert user.failed_login_attempts == 0 and user.locked_until is not None
    (locked,) = await audit_rows(session_factory, AuditAction.AUTH_ACCOUNT_LOCKED)
    assert ISO_LIMA.match(locked.details["lockedUntil"]) and locked.actor_user_id == admin_user.id

    # Con la contraseña correcta mientras está bloqueado: 423, sin verificar ni sumar intentos.
    correct = await login_as(bo_client, admin_user)
    assert correct.status_code == 423
    assert (await get_user(session_factory, admin_user.id)).failed_login_attempts == 0
    reasons = [
        row.details["reason"] for row in await audit_rows(session_factory, AuditAction.AUTH_LOGIN_FAILED)
    ]
    assert reasons == ["BAD_PASSWORD"] * 5 + ["LOCKED"]

    # Al vencer el bloqueo (simulado en la base): login correcto y contador en 0.
    await update_user(session_factory, admin_user.id, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    assert (await login_as(bo_client, admin_user)).status_code == 200
    user = await get_user(session_factory, admin_user.id)
    assert user.failed_login_attempts == 0 and user.locked_until is None


@pytest.mark.parametrize(
    ("remaining", "text"),
    [(timedelta(seconds=30), "en 1 minuto o"), (timedelta(seconds=61), "en 2 minutos o")],
)
async def test_locked_message_rounds_minutes_up(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    remaining: timedelta,
    text: str,
) -> None:
    await update_user(session_factory, admin_user.id, locked_until=datetime.now(UTC) + remaining)
    response = await login_as(bo_client, admin_user)
    assert response.status_code == 423
    assert text in response.json()["detail"]


async def test_failures_after_an_expired_lock_count_from_zero(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await update_user(session_factory, admin_user.id, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    assert (await login_as(bo_client, admin_user, password="Mala#Clave1")).status_code == 401
    assert (await get_user(session_factory, admin_user.id)).failed_login_attempts == 1


# --- B2.4 Usuario inactivo o eliminado ---


async def test_inactive_user(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await update_user(session_factory, admin_user.id, is_active=False)

    correct = await login_as(bo_client, admin_user)
    assert correct.status_code == 403
    assert correct.json() == {
        "detail": "Tu usuario está inactivo. Comunícate con el Administrador.",
        "code": "USER_INACTIVE",
    }
    wrong = await login_as(bo_client, admin_user, password="Mala#Clave1")
    assert wrong.status_code == 401 and wrong.json() == INVALID_CREDENTIALS
    reasons = [
        row.details["reason"] for row in await audit_rows(session_factory, AuditAction.AUTH_LOGIN_FAILED)
    ]
    assert reasons == ["INACTIVE", "BAD_PASSWORD"]


async def test_deleted_user_cannot_log_in(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await update_user(session_factory, admin_user.id, is_deleted=1, deleted_at=datetime.now(UTC))
    response = await login_as(bo_client, admin_user)
    assert response.status_code == 401 and response.json() == INVALID_CREDENTIALS
    (failed,) = await audit_rows(session_factory, AuditAction.AUTH_LOGIN_FAILED)
    assert failed.details == {"reason": "UNKNOWN_USER"} and failed.entity_id is None


# --- B2.5 CSRF ---


def backoffice_mutations() -> list[tuple[str, str]]:
    """Todo POST/PUT/PATCH/DELETE bajo /api/v1/backoffice (también los que agreguen las fases siguientes)."""
    from app.main import app

    spec = get_openapi(title="t", version="1", routes=app.routes)
    found = [
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        if path.startswith(BO)
        for method in operations
        if method in {"post", "put", "patch", "delete"}
    ]
    assert found
    return found


@pytest.mark.parametrize(("method", "path"), backoffice_mutations())
@pytest.mark.parametrize("headers", [{}, {"X-Requested-With": "fetch"}])
async def test_mutations_require_csrf_header(
    api_client: httpx.AsyncClient, admin_user: BoUser, method: str, path: str, headers: dict[str, str]
) -> None:
    """Con sesión válida (la cookie no basta) y sin body: la cabecera se valida antes que la sesión y el body."""
    from app.main import app

    path = re.sub(r"\{[^}]+\}", "6f0c5a1e-0000-4000-8000-000000000000", path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        logged = await client.post(
            f"{BO}/auth/login",
            json={"username": admin_user.username, "password": admin_user.password},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert logged.status_code == 200
        response = await client.request(method, path, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Solicitud no permitida.", "code": "CSRF_HEADER_MISSING"}


async def test_get_does_not_require_csrf_header(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(f"{BO}/auth/password-policy")
    assert response.status_code == 200


# --- B2.6 Sesión requerida, vencida o revocada ---


async def test_me_without_cookie(bo_client: httpx.AsyncClient) -> None:
    response = await me(bo_client)
    assert response.status_code == 401
    assert response.json() == {"detail": "Debes iniciar sesión.", "code": "AUTH_REQUIRED"}
    assert "set-cookie" not in response.headers


async def test_unknown_token_expires(bo_client: httpx.AsyncClient) -> None:
    bo_client.cookies.set("__session", "no-existe", path="/api/v1/backoffice")
    response = await me(bo_client)
    assert response.status_code == 401 and response.json() == SESSION_EXPIRED
    assert cookie_was_cleared(response)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"revoked_at": datetime.now(UTC), "revoked_reason": "LOGOUT"}, id="revocada"),
        pytest.param({"last_activity_at": datetime.now(UTC) - timedelta(minutes=30)}, id="inactividad"),
        pytest.param({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, id="maximo"),
    ],
)
async def test_expired_sessions(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions, values: dict[str, Any]
) -> None:
    await login_as(bo_client, admin_user)
    assert (await me(bo_client)).status_code == 200
    await update_sessions(session_factory, admin_user.id, **values)

    response = await me(bo_client)
    assert response.status_code == 401 and response.json() == SESSION_EXPIRED
    assert cookie_was_cleared(response)
    assert session_cookie(bo_client) is None
    assert (await me(bo_client)).json()["code"] == "AUTH_REQUIRED"


async def test_session_within_idle_time_is_valid(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await login_as(bo_client, admin_user)
    await update_sessions(
        session_factory, admin_user.id, last_activity_at=datetime.now(UTC) - timedelta(minutes=29)
    )
    assert (await me(bo_client)).status_code == 200


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"is_active": False}, "USER_DEACTIVATED"),
        ({"is_deleted": 1, "deleted_at": datetime.now(UTC)}, "USER_DELETED"),
    ],
)
async def test_session_of_inactive_or_deleted_user_is_revoked(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    values: dict[str, Any],
    reason: str,
) -> None:
    await login_as(bo_client, admin_user)
    await update_user(session_factory, admin_user.id, **values)

    response = await me(bo_client)
    assert response.status_code == 401 and response.json() == SESSION_EXPIRED
    assert cookie_was_cleared(response)
    (stored,) = await user_sessions(session_factory, admin_user.id)
    assert stored.revoked_at is not None and stored.revoked_reason == reason


# --- B2.7 Renovación de la actividad ---


async def test_last_activity_is_refreshed_at_most_once_per_minute(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await login_as(bo_client, admin_user)
    (initial,) = await user_sessions(session_factory, admin_user.id)
    await me(bo_client)
    await me(bo_client)
    (unchanged,) = await user_sessions(session_factory, admin_user.id)
    assert unchanged.last_activity_at == initial.last_activity_at

    almost = datetime.now(UTC) - timedelta(seconds=50)
    await update_sessions(session_factory, admin_user.id, last_activity_at=almost)
    await me(bo_client)
    (still,) = await user_sessions(session_factory, admin_user.id)
    assert still.last_activity_at == almost

    stale = datetime.now(UTC) - timedelta(seconds=61)
    await update_sessions(session_factory, admin_user.id, last_activity_at=stale)
    await me(bo_client)
    (refreshed,) = await user_sessions(session_factory, admin_user.id)
    assert refreshed.last_activity_at > datetime.now(UTC) - timedelta(seconds=5)


# --- Rutas protegidas de prueba (B2.8 y B2.11) ---


@pytest.fixture
async def protected_client(
    session_factory: Sessions, test_settings: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    """App aparte con el router del BackOffice y dos rutas de prueba: una que solo exige sesión y otra que exige el
    módulo USUARIOS. No modifica la app real."""
    test_app = FastAPI()
    test_app.add_middleware(BackofficeHeadersMiddleware, prefix=BO)
    test_app.add_exception_handler(BackofficeError, backoffice_error_handler)
    test_app.include_router(backoffice.router)
    router = APIRouter(prefix=f"{BO}/_pruebas")

    @router.get("/sesion")
    async def with_session(current: CurrentSession) -> dict[str, str]:
        return {"userId": str(current.user.id)}

    @router.get("/usuarios")
    async def users_module(
        current: Annotated[AuthenticatedSession, Depends(require_module(ModuleCode.USUARIOS))],
    ) -> dict[str, str]:
        return {"userId": str(current.user.id)}

    test_app.include_router(router)

    async def override_session() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    test_app.dependency_overrides[get_session] = override_session
    test_app.dependency_overrides[get_settings] = lambda: test_settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
        headers={"X-Requested-With": "XMLHttpRequest"},
    ) as client:
        yield client


async def test_pending_password_change_blocks_protected_endpoints(
    protected_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    user = await create_bo_user(
        session_factory,
        username="nuevo.qa",
        dni="10293847",
        email="nuevo.qa@correo.test",
        role_code=RoleCode.ADMIN,
        must_change_password=True,
    )
    logged = await login_as(protected_client, user)
    assert logged.status_code == 200 and logged.json()["mustChangePassword"] is True
    assert (await me(protected_client)).json()["mustChangePassword"] is True

    for path in ("/_pruebas/sesion", "/_pruebas/usuarios"):
        blocked = await protected_client.get(f"{BO}{path}")
        assert blocked.status_code == 403
        assert blocked.json() == {
            "detail": "Debes cambiar tu contraseña antes de continuar.",
            "code": "PASSWORD_CHANGE_REQUIRED",
        }

    changed = await protected_client.post(
        f"{BO}/auth/change-password", json={"currentPassword": user.password, "newPassword": "Ñandú#2026"}
    )
    assert changed.status_code == 204
    assert (await protected_client.get(f"{BO}/_pruebas/sesion")).status_code == 200
    assert (await me(protected_client)).json()["mustChangePassword"] is False
    (audit,) = await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_CHANGED)
    assert audit.details == {"forced": True}


async def test_require_module_and_role_change_on_next_request(
    protected_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    await login_as(protected_client, operator_user)
    assert (await protected_client.get(f"{BO}/_pruebas/sesion")).status_code == 200
    forbidden = await protected_client.get(f"{BO}/_pruebas/usuarios")
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "No tienes permiso para realizar esta acción.", "code": "FORBIDDEN"}

    # Cambiar el rol en la base aplica en la siguiente petición, sin volver a iniciar sesión.
    await update_user(session_factory, operator_user.id, role_code=RoleCode.ADMIN.value)
    assert (await protected_client.get(f"{BO}/_pruebas/usuarios")).status_code == 200
    assert [m["code"] for m in (await me(protected_client)).json()["modules"]] == [
        "DEALERS",
        "USUARIOS",
        "AUDITORIA",
    ]
    await update_user(session_factory, operator_user.id, role_code=RoleCode.OPERADOR.value)
    assert (await protected_client.get(f"{BO}/_pruebas/usuarios")).status_code == 403


async def test_admin_has_users_module(protected_client: httpx.AsyncClient, admin_user: BoUser) -> None:
    await login_as(protected_client, admin_user)
    response = await protected_client.get(f"{BO}/_pruebas/usuarios")
    assert response.status_code == 200 and response.json() == {"userId": str(admin_user.id)}


async def test_protected_route_without_session(protected_client: httpx.AsyncClient) -> None:
    response = await protected_client.get(f"{BO}/_pruebas/usuarios")
    assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"


# --- B2.9 Cambio de contraseña ---


async def change_password(client: httpx.AsyncClient, current: str, new: str) -> httpx.Response:
    return await client.post(
        f"{BO}/auth/change-password", json={"currentPassword": current, "newPassword": new}
    )


async def test_change_password_with_wrong_current(
    bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions
) -> None:
    await login_as(bo_client, admin_user)
    response = await change_password(bo_client, "Mala#Clave1", "Nueva#Clave9")
    assert response.status_code == 422
    assert response.json() == {
        "detail": "La contraseña actual no es correcta.",
        "code": "CURRENT_PASSWORD_INVALID",
        "field": "currentPassword",
    }
    # No suma intentos de login ni cambia nada.
    user = await get_user(session_factory, admin_user.id)
    assert user.failed_login_attempts == 0 and user.password_changed_at is None
    assert await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_CHANGED) == []


@pytest.mark.parametrize(
    ("new_password", "requirement"),
    [
        pytest.param("Abc#123", "al menos 8 caracteres", id="largo-7"),
        pytest.param("Abcdefgh#1234567890x", None, id="largo-20-valido"),
        pytest.param("Abcdefgh#1234567890xy", "como máximo 20 caracteres", id="largo-21"),
        pytest.param("abcdef#123", "una letra mayúscula", id="sin-mayuscula"),
        pytest.param("ABCDEF#123", "una letra minúscula", id="sin-minuscula"),
        pytest.param("Abcdefg#hi", "un número", id="sin-numero"),
        pytest.param("Abcdefg123", "un carácter especial (por ejemplo ! @ # $ %)", id="sin-especial"),
        pytest.param("X#1JPEREZ.QAx", "no debe contener tu usuario ni tu DNI", id="con-usuario"),
        pytest.param("Ab#45871236", "no debe contener tu usuario ni tu DNI", id="con-dni"),
        pytest.param(DEFAULT_PASSWORD, "distinta de la contraseña actual", id="igual-a-la-actual"),
        pytest.param(" Abcdef#123", "sin espacios al inicio ni al final", id="espacio-al-inicio"),
        pytest.param("Abcdef#123 ", "sin espacios al inicio ni al final", id="espacio-al-final"),
        pytest.param("", "al menos 8 caracteres", id="vacia"),
        pytest.param("Ñandú#2026", None, id="enie-y-tilde"),
        pytest.param("Abc def#123", None, id="espacio-intermedio"),
    ],
)
async def test_change_password_policy(
    bo_client: httpx.AsyncClient, admin_user: BoUser, new_password: str, requirement: str | None
) -> None:
    await login_as(bo_client, admin_user)
    response = await change_password(bo_client, admin_user.password, new_password)
    if requirement is None:
        assert response.status_code == 204, response.text
        return
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "PASSWORD_POLICY" and body["field"] == "newPassword"
    assert body["detail"].startswith("La contraseña no cumple los requisitos: ") and body["detail"].endswith(
        "."
    )
    assert requirement in body["detail"]


async def test_policy_detail_lists_only_unmet_requirements(
    bo_client: httpx.AsyncClient, admin_user: BoUser
) -> None:
    await login_as(bo_client, admin_user)
    response = await change_password(bo_client, admin_user.password, "abc")
    assert response.json()["detail"] == (
        "La contraseña no cumple los requisitos: al menos 8 caracteres, una letra mayúscula, un número, "
        "un carácter especial (por ejemplo ! @ # $ %)."
    )


async def test_change_password_revokes_other_sessions_and_rotates_the_current_one(
    make_bo_client: Callable[[], httpx.AsyncClient], admin_user: BoUser, session_factory: Sessions
) -> None:
    current, other = make_bo_client(), make_bo_client()
    await login_as(current, admin_user)
    await login_as(other, admin_user)
    old_token = session_cookie(current)
    async with session_factory.begin() as session:
        session.add(
            PasswordResetToken(
                user_id=admin_user.id,
                token_hash=hash_token("enlace-pendiente"),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )

    response = await change_password(current, admin_user.password, "Nueva#Clave9")

    assert response.status_code == 204
    new_token = session_cookie(current)
    assert new_token and new_token != old_token
    assert "Max-Age=43200" in set_cookie_header(response)
    assert (await me(current)).status_code == 200
    assert (await me(other)).status_code == 401
    # El token anterior de la sesión actual ya no sirve.
    current.cookies.set("__session", old_token, path="/api/v1/backoffice")
    assert (await me(current)).status_code == 401

    stored = await user_sessions(session_factory, admin_user.id)
    assert sorted(s.revoked_reason or "VIGENTE" for s in stored) == [
        "PASSWORD_CHANGED",
        "PASSWORD_CHANGED",
        "VIGENTE",
    ]
    user = await get_user(session_factory, admin_user.id)
    assert user.password_changed_at is not None and user.must_change_password is False
    assert passwords.verify_password(user.password_hash, "Nueva#Clave9")
    (audit,) = await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_CHANGED)
    assert audit.details == {"forced": False} and audit.actor_user_id == admin_user.id
    (token,) = await reset_tokens(session_factory, admin_user.id)
    assert token.invalidated_at is not None  # sección 4.3: se invalida al cambiar la contraseña
    # Login con la nueva contraseña; la anterior ya no sirve.
    assert (await login_as(make_bo_client(), admin_user, password="Nueva#Clave9")).status_code == 200
    assert (await login_as(make_bo_client(), admin_user)).status_code == 401


async def test_change_password_requires_session(bo_client: httpx.AsyncClient) -> None:
    response = await change_password(bo_client, "x", "Nueva#Clave9")
    assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"


# --- B2.10 Cierre de sesión ---


async def test_logout(bo_client: httpx.AsyncClient, admin_user: BoUser, session_factory: Sessions) -> None:
    await login_as(bo_client, admin_user)

    response = await bo_client.post(f"{BO}/auth/logout")

    assert response.status_code == 204
    assert cookie_was_cleared(response)
    (stored,) = await user_sessions(session_factory, admin_user.id)
    assert stored.revoked_at is not None and stored.revoked_reason == "LOGOUT"
    (audit,) = await audit_rows(session_factory, AuditAction.AUTH_LOGOUT)
    assert audit.actor_user_id == admin_user.id
    assert (await me(bo_client)).status_code == 401

    # Idempotente: sin cookie, o con la cookie de una sesión ya revocada.
    again = await bo_client.post(f"{BO}/auth/logout")
    assert again.status_code == 204 and cookie_was_cleared(again)
    bo_client.cookies.set("__session", "revocada", path="/api/v1/backoffice")
    assert (await bo_client.post(f"{BO}/auth/logout")).status_code == 204
    assert len(await audit_rows(session_factory, AuditAction.AUTH_LOGOUT)) == 1


async def test_logout_with_pending_password_change(
    bo_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    user = await create_bo_user(
        session_factory, username="nuevo.qa", dni="10293847", email="n@correo.test", must_change_password=True
    )
    await login_as(bo_client, user)
    assert (await bo_client.post(f"{BO}/auth/logout")).status_code == 204
    (stored,) = await user_sessions(session_factory, user.id)
    assert stored.revoked_reason == "LOGOUT"


# --- B2.12 Cabeceras ---


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/auth/password-policy", 200),
        ("GET", "/auth/me", 401),
        ("POST", "/auth/login", 422),
        ("GET", "/no-existe", 404),
    ],
)
async def test_backoffice_responses_are_not_cached(
    bo_client: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    response = await bo_client.request(method, f"{BO}{path}", json={} if method == "POST" else None)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_other_endpoints_keep_their_headers(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/api/v1/public/consent-links/no-existe")
    assert response.status_code == 404
    assert "cache-control" not in response.headers


async def test_password_policy(bo_client: httpx.AsyncClient, test_settings: Settings) -> None:
    response = await bo_client.get(f"{BO}/auth/password-policy")
    assert response.json() == {
        "minLength": 8,
        "maxLength": 20,
        "requireUppercase": True,
        "requireLowercase": True,
        "requireDigit": True,
        "requireSpecial": True,
    }
    test_settings.backoffice_password_min_length = 12
    assert (await bo_client.get(f"{BO}/auth/password-policy")).json()["minLength"] == 12


# --- B2.13 Rehash ---


async def test_login_rehashes_with_current_parameters(
    bo_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    old_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(DEFAULT_PASSWORD)
    user = await create_bo_user(
        session_factory, username="antiguo.qa", dni="11223344", email="a@correo.test", password_hash=old_hash
    )
    assert passwords.needs_rehash(old_hash)

    assert (await login_as(bo_client, user)).status_code == 200

    stored = (await get_user(session_factory, user.id)).password_hash
    assert stored != old_hash and stored.startswith("$argon2id$")
    assert not passwords.needs_rehash(stored)
    assert (await login_as(bo_client, user)).status_code == 200


# --- B2.15 Nada sensible en los logs ---


async def test_logs_have_no_secrets_or_personal_data(
    make_bo_client: Callable[[], httpx.AsyncClient],
    admin_user: BoUser,
    session_factory: Sessions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_bo_client()
    with caplog.at_level(logging.DEBUG):
        await client.post(f"{BO}/auth/login", json={"username": "fantasma.qa", "password": "Otra#Clave1"})
        for _ in range(5):
            await login_as(client, admin_user, password="Mala#Clave1")
        await update_user(session_factory, admin_user.id, locked_until=None)
        await login_as(client, admin_user)
        token = session_cookie(client)
        await me(client)
        await change_password(client, "Mala#Clave1", "Nueva#Clave9")
        await change_password(client, admin_user.password, "Nueva#Clave9")
        new_token = session_cookie(client)
        await client.post(f"{BO}/auth/logout")

    assert caplog.records
    text = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    for secret in (
        admin_user.password,
        "Mala#Clave1",
        "Nueva#Clave9",
        "Otra#Clave1",
        token,
        new_token,
        admin_user.dni,
        admin_user.email,
        admin_user.username,
        "fantasma",
        "987654321",
        "$argon2id$",
    ):
        assert secret and secret not in text, secret


async def test_request_log_has_the_full_route_template(
    bo_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        await bo_client.get(f"{BO}/auth/password-policy")
    (record,) = [r for r in caplog.records if r.name == "app.request"]
    assert record.path == "/api/v1/backoffice/auth/password-policy"
