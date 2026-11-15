"""Recuperación de contraseña por correo (BACKOFFICE_DEALERS.spec.md v1.2, sección 6.3; criterios B3.3–B3.6).

Los correos no se envían: `RecordingEmailSender` (conftest) los guarda. Los tiempos se simulan en la base.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from app.core.config import Settings
from app.core.passwords import verify_password
from app.models import AuditAction, PasswordResetToken
from app.services.backoffice.email_templates import CHANGED_SUBJECT, RESET_SUBJECT
from app.utils.tokens import hash_token
from tests.bo_helpers import (
    BO,
    BoUser,
    Sessions,
    audit_rows,
    get_user,
    login_as,
    reset_tokens,
    update_user,
    user_sessions,
)
from tests.conftest import RecordingEmailSender

BASE_URL = "https://bo.prueba.test"
FORGOT_DETAIL = {
    "detail": "Si los datos corresponden a un usuario activo, te enviaremos un correo con un enlace para restablecer "
    "tu contraseña. El enlace vence en 30 minutos."
}
LINK = re.compile(rf"{re.escape(BASE_URL)}/restablecer-contrasena\?t=([A-Za-z0-9_-]{{43}})")
NEW_PASSWORD = "Nueva#Clave9"


@pytest.fixture(autouse=True)
def backoffice_base_url(test_settings: Settings) -> None:
    test_settings.backoffice_base_url = BASE_URL


async def forgot(client: httpx.AsyncClient, identifier: str) -> httpx.Response:
    return await client.post(f"{BO}/auth/forgot-password", json={"identifier": identifier})


async def reset(client: httpx.AsyncClient, token: str, new_password: str = NEW_PASSWORD) -> httpx.Response:
    return await client.post(f"{BO}/auth/password-reset", json={"token": token, "newPassword": new_password})


async def status_of(client: httpx.AsyncClient, token: str) -> str | int:
    response = await client.get(f"{BO}/auth/password-reset/{token}")
    if response.status_code != 200:
        return response.status_code
    assert set(response.json()) == {"tokenStatus"}  # sin datos del usuario
    return response.json()["tokenStatus"]


def token_from(email_sender: RecordingEmailSender, index: int = -1) -> str:
    message = email_sender.messages[index]
    match = LINK.search(message.text)
    assert match, message.text
    return match[1]


async def request_token(client: httpx.AsyncClient, email_sender: RecordingEmailSender, user: BoUser) -> str:
    assert (await forgot(client, user.username)).status_code == 202
    return token_from(email_sender)


# --- B3.3 Solicitud válida ---


@pytest.mark.parametrize(
    "identifier", ["jperez.qa", "  JPEREZ.QA ", "jperez.qa@correo.test", "JPerez.QA@Correo.TEST"]
)
async def test_forgot_password_with_username_or_email(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
    identifier: str,
) -> None:
    response = await forgot(bo_client, identifier)

    assert response.status_code == 202 and response.json() == FORGOT_DETAIL
    (message,) = email_sender.messages
    assert message.to == admin_user.email and message.subject == RESET_SUBJECT
    token = token_from(email_sender)
    assert f"{BASE_URL}/restablecer-contrasena?t={token}" in message.html
    (stored,) = await reset_tokens(session_factory, admin_user.id)
    assert stored.token_hash == hash_token(token) != token
    assert stored.expires_at - stored.created_at == timedelta(minutes=30)
    assert str(stored.requested_ip) == "127.0.0.1" and stored.requested_user_agent == "pytest-backoffice"
    assert stored.used_at is None and stored.invalidated_at is None
    (audit,) = await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_RESET_REQUESTED)
    assert audit.actor_user_id is None  # acción anónima
    assert (audit.entity_type, audit.entity_id) == ("USER", str(admin_user.id))
    assert await status_of(bo_client, token) == "VALID"


async def test_new_request_invalidates_previous_links(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
) -> None:
    first = await request_token(bo_client, email_sender, admin_user)
    second = await request_token(bo_client, email_sender, admin_user)

    assert await status_of(bo_client, first) == "INVALID"
    assert await status_of(bo_client, second) == "VALID"
    stored = await reset_tokens(session_factory, admin_user.id)
    assert [t.invalidated_at is not None for t in stored] == [True, False]


async def test_locked_user_can_request_a_link(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
) -> None:
    await update_user(session_factory, admin_user.id, locked_until=datetime.now(UTC) + timedelta(minutes=10))
    assert (await forgot(bo_client, admin_user.username)).status_code == 202
    assert len(email_sender.messages) == 1


async def test_ttl_comes_from_settings(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
    test_settings: Settings,
) -> None:
    test_settings.backoffice_password_reset_ttl_minutes = 1
    response = await forgot(bo_client, admin_user.username)
    assert response.json()["detail"].endswith("El enlace vence en 1 minuto.")
    assert "Vence en 1 minuto y solo puede usarse una vez" in email_sender.messages[0].text
    (stored,) = await reset_tokens(session_factory, admin_user.id)
    assert stored.expires_at - stored.created_at == timedelta(minutes=1)


# --- B3.4 Sin token ni correo, con la misma respuesta ---


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(None, id="inexistente"),
        pytest.param({"is_active": False}, id="inactivo"),
        pytest.param({"is_deleted": 1, "deleted_at": datetime.now(UTC)}, id="eliminado"),
    ],
)
async def test_forgot_password_reveals_nothing(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
    values: dict[str, Any] | None,
) -> None:
    identifier = "nadie.qa"
    if values is not None:
        await update_user(session_factory, admin_user.id, **values)
        identifier = admin_user.email

    response = await forgot(bo_client, identifier)

    assert response.status_code == 202 and response.json() == FORGOT_DETAIL
    assert email_sender.messages == []
    assert await reset_tokens(session_factory, admin_user.id) == []
    assert await audit_rows(session_factory) == []


async def test_hourly_limit(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for _ in range(3):
        assert (await forgot(bo_client, admin_user.username)).status_code == 202

    with caplog.at_level(logging.WARNING, logger="app.services.backoffice.password_reset"):
        fourth = await forgot(bo_client, admin_user.username)

    assert fourth.status_code == 202 and fourth.json() == FORGOT_DETAIL
    assert len(email_sender.messages) == 3
    assert len(await reset_tokens(session_factory, admin_user.id)) == 3
    assert len(await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_RESET_REQUESTED)) == 3
    (record,) = [
        r for r in caplog.records if r.name.endswith("password_reset") and r.levelno >= logging.WARNING
    ]
    assert record.levelname == "WARNING" and record.user_id == str(admin_user.id)
    # El tercer enlace sigue vigente: el cuarto pedido no invalida nada.
    assert await status_of(bo_client, token_from(email_sender)) == "VALID"

    # Los enlaces de hace más de una hora no cuentan.
    async with session_factory.begin() as session:
        await session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == admin_user.id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=61))
        )
    assert (await forgot(bo_client, admin_user.username)).status_code == 202
    assert len(email_sender.messages) == 4


@pytest.mark.parametrize("body", [{"identifier": ""}, {"identifier": "   "}, {"identifier": "a" * 255}, {}])
async def test_forgot_password_validation(bo_client: httpx.AsyncClient, body: dict[str, str]) -> None:
    assert (await bo_client.post(f"{BO}/auth/forgot-password", json=body)).status_code == 422


# --- B3.5 Estado del enlace ---


async def test_token_statuses(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
) -> None:
    token = await request_token(bo_client, email_sender, admin_user)
    assert await status_of(bo_client, token) == "VALID"
    assert await status_of(bo_client, "no-existe") == 404
    not_found = await bo_client.get(f"{BO}/auth/password-reset/no-existe")
    assert not_found.json() == {"detail": "El enlace no existe.", "code": "RESET_LINK_NOT_FOUND"}

    async def set_token(**values: Any) -> None:
        async with session_factory.begin() as session:
            await session.execute(
                update(PasswordResetToken)
                .where(PasswordResetToken.token_hash == hash_token(token))
                .values(**values)
            )

    past = datetime.now(UTC) - timedelta(seconds=1)
    await set_token(expires_at=past)
    assert await status_of(bo_client, token) == "EXPIRED"
    # Prioridad: USED > INVALID > EXPIRED (como en la landing, usado gana a vencido).
    await set_token(invalidated_at=past)
    assert await status_of(bo_client, token) == "INVALID"
    await set_token(used_at=past)
    assert await status_of(bo_client, token) == "USED"


@pytest.mark.parametrize(
    "values",
    [{"is_active": False}, {"is_deleted": 1, "deleted_at": datetime.now(UTC)}],
    ids=["inactivo", "eliminado"],
)
async def test_token_of_inactive_or_deleted_user_is_invalid(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
    values: dict[str, Any],
) -> None:
    token = await request_token(bo_client, email_sender, admin_user)
    await update_user(session_factory, admin_user.id, **values)
    assert await status_of(bo_client, token) == "INVALID"
    response = await reset(bo_client, token)
    assert response.status_code == 410


# --- B3.6 Restablecer ---


async def test_password_reset(
    make_bo_client: Callable[[], httpx.AsyncClient],
    session_factory: Sessions,
    admin_user: BoUser,
    email_sender: RecordingEmailSender,
) -> None:
    logged = make_bo_client()
    await login_as(logged, admin_user)
    client = make_bo_client()
    token = await request_token(client, email_sender, admin_user)
    async with session_factory.begin() as session:
        session.add(
            PasswordResetToken(
                user_id=admin_user.id,
                token_hash=hash_token("otro-enlace"),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
    await update_user(
        session_factory,
        admin_user.id,
        failed_login_attempts=4,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
        must_change_password=True,
    )

    response = await reset(client, token)

    assert response.status_code == 204, response.text
    assert "set-cookie" not in response.headers  # no inicia sesión
    user = await get_user(session_factory, admin_user.id)
    assert verify_password(user.password_hash, NEW_PASSWORD)
    assert user.failed_login_attempts == 0 and user.locked_until is None
    assert user.must_change_password is False and user.password_changed_at is not None
    assert [s.revoked_reason for s in await user_sessions(session_factory, admin_user.id)] == [
        "PASSWORD_RESET"
    ]
    assert (await logged.get(f"{BO}/auth/me")).status_code == 401
    tokens = {t.token_hash: t for t in await reset_tokens(session_factory, admin_user.id)}
    assert tokens[hash_token(token)].used_at is not None
    assert tokens[hash_token("otro-enlace")].invalidated_at is not None
    assert await status_of(client, token) == "USED"
    (audit,) = await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_RESET_COMPLETED)
    assert audit.actor_user_id is None and audit.entity_id == str(admin_user.id)

    # Aviso "Tu contraseña fue actualizada".
    notice = email_sender.messages[-1]
    assert (notice.to, notice.subject) == (admin_user.email, CHANGED_SUBJECT)
    assert "La contraseña de tu usuario jperez.qa fue actualizada el " in notice.text

    assert (await login_as(make_bo_client(), admin_user)).status_code == 401
    assert (await login_as(make_bo_client(), admin_user, password=NEW_PASSWORD)).status_code == 200
    # El mismo enlace no sirve dos veces.
    assert (await reset(client, token, "Otra#Clave22")).status_code == 410


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, id="vencido"),
        pytest.param({"used_at": datetime.now(UTC)}, id="usado"),
        pytest.param({"invalidated_at": datetime.now(UTC)}, id="invalidado"),
    ],
)
async def test_password_reset_with_unusable_link(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
    values: dict[str, Any],
) -> None:
    token = await request_token(bo_client, email_sender, admin_user)
    async with session_factory.begin() as session:
        await session.execute(update(PasswordResetToken).values(**values))

    response = await reset(bo_client, token)

    assert response.status_code == 410
    assert response.json() == {
        "detail": "El enlace para restablecer la contraseña no es válido o ya venció. Solicita uno nuevo.",
        "code": "RESET_LINK_INVALID",
    }
    assert not verify_password((await get_user(session_factory, admin_user.id)).password_hash, NEW_PASSWORD)
    assert len(email_sender.messages) == 1  # solo el del enlace
    assert await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_RESET_COMPLETED) == []


async def test_password_reset_with_unknown_link(bo_client: httpx.AsyncClient) -> None:
    response = await reset(bo_client, "no-existe")
    assert response.status_code == 404
    assert response.json() == {"detail": "El enlace no existe.", "code": "RESET_LINK_NOT_FOUND"}


@pytest.mark.parametrize(
    ("new_password", "requirement"),
    [
        ("corta#1", "al menos 8 caracteres"),
        ("Abcdefgh#1234567890xy", "como máximo 20 caracteres"),
        ("X#1jperez.qaX", "no debe contener tu usuario ni tu DNI"),
        ("Clave#2026x", "distinta de la contraseña actual"),  # DEFAULT_PASSWORD
    ],
)
async def test_password_reset_policy(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    email_sender: RecordingEmailSender,
    new_password: str,
    requirement: str,
) -> None:
    token = await request_token(bo_client, email_sender, admin_user)

    response = await reset(bo_client, token, new_password)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "PASSWORD_POLICY" and body["field"] == "newPassword"
    assert requirement in body["detail"]
    # El enlace no se consume: se puede reintentar con una contraseña válida.
    assert await status_of(bo_client, token) == "VALID"
    assert (await reset(bo_client, token)).status_code == 204


async def test_concurrent_resets_only_one_wins(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    session_factory: Sessions,
    email_sender: RecordingEmailSender,
) -> None:
    token = await request_token(bo_client, email_sender, admin_user)

    responses = await asyncio.gather(
        reset(bo_client, token, "Primera#Clave1"), reset(bo_client, token, "Segunda#Clave2")
    )

    assert sorted(r.status_code for r in responses) == [204, 410]
    winner = "Primera#Clave1" if responses[0].status_code == 204 else "Segunda#Clave2"
    assert verify_password((await get_user(session_factory, admin_user.id)).password_hash, winner)
    assert len(await audit_rows(session_factory, AuditAction.AUTH_PASSWORD_RESET_COMPLETED)) == 1
    assert [m.subject for m in email_sender.messages] == [RESET_SUBJECT, CHANGED_SUBJECT]


@pytest.mark.parametrize(
    "body", [{"token": "", "newPassword": "x"}, {"token": "x"}, {"token": "x", "newPassword": "x", "a": 1}]
)
async def test_password_reset_validation(bo_client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    assert (await bo_client.post(f"{BO}/auth/password-reset", json=body)).status_code == 422


# --- Aviso en change-password (tarea de B3) ---


async def test_change_password_sends_notice(
    bo_client: httpx.AsyncClient, admin_user: BoUser, email_sender: RecordingEmailSender
) -> None:
    await login_as(bo_client, admin_user)
    failed = await bo_client.post(
        f"{BO}/auth/change-password", json={"currentPassword": "Mala#Clave1", "newPassword": NEW_PASSWORD}
    )
    assert failed.status_code == 422 and email_sender.messages == []

    response = await bo_client.post(
        f"{BO}/auth/change-password",
        json={"currentPassword": admin_user.password, "newPassword": NEW_PASSWORD},
    )

    assert response.status_code == 204
    (notice,) = email_sender.messages
    assert (notice.to, notice.subject) == (admin_user.email, CHANGED_SUBJECT)
    assert re.search(r"fue actualizada el \d{2}/\d{2}/\d{4} \d{2}:\d{2} \(hora de Lima\)", notice.text)


# --- Nada sensible en los logs (B2.15 aplicado a B3) ---


async def test_reset_flow_logs_have_no_secrets(
    bo_client: httpx.AsyncClient,
    admin_user: BoUser,
    email_sender: RecordingEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await forgot(bo_client, admin_user.email)
        await forgot(bo_client, "nadie@correo.test")
        token = token_from(email_sender)
        await status_of(bo_client, token)
        await reset(bo_client, token, "corta")
        await reset(bo_client, token)

    # Solo los logs de la app: el logger "httpx" es el del cliente de prueba (escribe la URL que pidió el test).
    app_records = [r for r in caplog.records if not r.name.startswith("httpx")]
    text = " ".join(r.getMessage() for r in app_records) + str([vars(r) for r in app_records])
    for secret in (token, hash_token(token), admin_user.email, "nadie@", admin_user.username, NEW_PASSWORD):
        assert secret not in text, secret
    paths = [r.path for r in caplog.records if r.name == "app.request"]
    assert "/api/v1/backoffice/auth/password-reset/{token}" in paths


async def test_unique_token_per_request(
    bo_client: httpx.AsyncClient, admin_user: BoUser, email_sender: RecordingEmailSender
) -> None:
    tokens = {await request_token(bo_client, email_sender, admin_user) for _ in range(3)}
    assert len(tokens) == 3
