"""CLI del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, secciones 5.11 y 5.13; criterios B2.14 y B2.16).

La CLI corre en el mismo proceso contra la base de pruebas (`run(..., session_factory=...)`). Un test crea un rol
temporal sin DELETE sobre audit_logs (como `consent_app`) para comprobar el error de permisos de `purge`; lo elimina
al terminar.
"""

import io
import logging
import os
import re
import secrets
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, make_url, pool, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cli import backoffice as cli
from app.core.config import Settings
from app.core.passwords import verify_password
from app.models import AuditAction, AuditLog, PasswordResetToken, User, UserSession
from app.utils.tokens import hash_token
from tests.bo_helpers import (
    BoUser,
    Sessions,
    audit_rows,
    create_bo_user,
    get_user,
    login_as,
    reset_tokens,
    update_user,
    user_sessions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMIN_ARGS = [
    "create-admin",
    "--dni",
    "00000000",
    "--first-names",
    "Administrador",
    "--last-names",
    "General",
    "--username",
    "admin",
    "--email",
    "admin@backoffice.local",
]
OUTPUT = re.compile(
    r"^Usuario: (?P<username>\S+) \((?P<role>[^)]+)\)\n"
    r"Contraseña temporal \(se muestra una sola vez\): (?P<password>\S{20})\n"
    r"Debe cambiarla en el primer inicio de sesión\.\n$"
)


@pytest.fixture
def run_cli(session_factory: Sessions, test_settings: Settings) -> Callable[..., object]:
    async def run(*argv: str, sessions: Sessions | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = await cli.run(
            argv, session_factory=sessions or session_factory, settings=test_settings, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    return run


async def count_users(sessions: Sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(User))


# --- create-admin (B2.14) ---


async def test_create_admin(
    run_cli: Callable[..., object], session_factory: Sessions, bo_client: httpx.AsyncClient
) -> None:
    code, out, err = await run_cli(*ADMIN_ARGS)

    assert (code, err) == (0, "")
    match = OUTPUT.match(out)
    assert match and match["username"] == "admin" and match["role"] == "Administrador"
    temporary = match["password"]

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
    assert user is not None
    assert (user.role_code, user.dni, user.first_names, user.last_names, user.email, user.phone) == (
        "ADMIN",
        "00000000",
        "Administrador",
        "General",
        "admin@backoffice.local",
        None,
    )
    assert user.must_change_password and user.is_active and user.created_by is None
    assert verify_password(user.password_hash, temporary) and temporary not in user.password_hash
    (audit,) = await audit_rows(session_factory, AuditAction.USER_CREATED)
    assert audit.actor_user_id is None and audit.entity_id == str(user.id)
    assert audit.details == {
        "dni": "00000000",
        "firstNames": "Administrador",
        "lastNames": "General",
        "username": "admin",
        "email": "admin@backoffice.local",
        "phone": None,
        "roleCode": "ADMIN",
        "source": "CLI",
    }

    # Ingresa con la contraseña temporal y debe cambiarla.
    logged = await bo_client.post(
        "/api/v1/backoffice/auth/login", json={"username": "admin", "password": temporary}
    )
    assert logged.status_code == 200 and logged.json()["mustChangePassword"] is True


async def test_create_admin_normalizes_values(
    run_cli: Callable[..., object], session_factory: Sessions
) -> None:
    code, _, _ = await run_cli(
        "create-admin",
        "--dni",
        "45871236",
        "--first-names",
        "  juan   carlos ",
        "--last-names",
        "Pérez Gómez",
        "--username",
        " JPerez ",
        "--email",
        " JPerez@Dominio.com ",
        "--phone",
        "+51 987 654 321",
    )
    assert code == 0
    async with session_factory() as session:
        user = await session.scalar(select(User))
    assert (user.first_names, user.username, user.email, user.phone) == (
        "juan carlos",
        "jperez",
        "jperez@dominio.com",
        "987654321",
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"--dni": "00000000"}, "Ya existe un usuario con ese DNI."),
        ({"--username": "ADMIN"}, "Ya existe un usuario con ese nombre de usuario."),
        ({"--email": "Admin@BackOffice.local"}, "Ya existe un usuario con ese correo."),
    ],
)
async def test_create_admin_rejects_duplicates(
    run_cli: Callable[..., object], session_factory: Sessions, overrides: dict[str, str], message: str
) -> None:
    assert (await run_cli(*ADMIN_ARGS))[0] == 0
    values = {
        "--dni": "11111111",
        "--username": "otro.admin",
        "--email": "otro@backoffice.local",
        **overrides,
    }
    args = list(ADMIN_ARGS)
    for flag, value in values.items():
        args[args.index(flag) + 1] = value

    code, out, err = await run_cli(*args)

    assert (code, out, err) == (1, "", f"{message}\n")
    assert await count_users(session_factory) == 1


async def test_deleted_user_frees_its_values(
    run_cli: Callable[..., object], session_factory: Sessions
) -> None:
    assert (await run_cli(*ADMIN_ARGS))[0] == 0
    async with session_factory.begin() as session:
        user = await session.scalar(select(User))
        user.is_deleted, user.deleted_at = 1, datetime.now(UTC)
    assert (await run_cli(*ADMIN_ARGS))[0] == 0
    assert await count_users(session_factory) == 2


async def test_create_admin_rejects_invalid_data(
    run_cli: Callable[..., object], session_factory: Sessions
) -> None:
    code, out, err = await run_cli(
        "create-admin",
        "--dni",
        "123",
        "--first-names",
        "Juan2",
        "--last-names",
        "Pérez",
        "--username",
        "1admin",
        "--email",
        "no-es-correo",
        "--phone",
        "12345",
    )
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        "--dni: El DNI debe tener 8 dígitos.",
        "--first-names: El nombre solo puede contener letras, espacios, apóstrofo, punto y guion.",
        (
            "--username: El usuario debe tener de 4 a 30 caracteres: letras minúsculas, números, punto, guion o guion "
            "bajo, y empezar con una letra."
        ),
        "--email: El correo no tiene un formato válido.",
        "--phone: El celular debe tener 9 dígitos.",
    ]
    assert await count_users(session_factory) == 0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["create-admin", "--dni", "00000000"], ("Faltan argumentos obligatorios: --first-names, --last-names, "
                                                "--username, --email")),
        (["reset-password", "--username"], "El argumento --username necesita un valor"),
        (["purge", "--audit-days", "x"], "El argumento --audit-days debe ser un número entero"),
        (["purge", "--force"], "Argumentos no reconocidos: --force"),
        (["borrar-todo"], "Comando no reconocido. Use create-admin, reset-password o purge"),
        ([], "Faltan argumentos obligatorios: command"),
    ],
)  # fmt: skip
async def test_argument_errors_in_spanish(
    run_cli: Callable[..., object], argv: list[str], message: str
) -> None:
    assert await run_cli(*argv) == (1, "", f"{message}\n")


# --- reset-password (B2.14) ---


async def test_reset_password_revokes_sessions_and_unlocks(
    run_cli: Callable[..., object],
    session_factory: Sessions,
    admin_user: BoUser,
    make_bo_client: Callable[[], httpx.AsyncClient],
) -> None:
    first, second = make_bo_client(), make_bo_client()
    await login_as(first, admin_user)
    await login_as(second, admin_user)
    await update_user(
        session_factory,
        admin_user.id,
        failed_login_attempts=3,
        locked_until=datetime.now(UTC) + timedelta(minutes=9),
    )
    async with session_factory.begin() as session:
        session.add(
            PasswordResetToken(
                user_id=admin_user.id,
                token_hash=hash_token("pendiente"),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )

    code, out, err = await run_cli("reset-password", "--username", " JPEREZ.QA ")

    assert (code, err) == (0, "")
    match = OUTPUT.match(out)
    assert match and match["username"] == "jperez.qa" and match["role"] == "Administrador"
    user = await get_user(session_factory, admin_user.id)
    assert user.must_change_password and user.failed_login_attempts == 0 and user.locked_until is None
    assert verify_password(user.password_hash, match["password"])
    assert [s.revoked_reason for s in await user_sessions(session_factory, admin_user.id)] == [
        "ADMIN_PASSWORD_RESET",
        "ADMIN_PASSWORD_RESET",
    ]
    assert (await first.get("/api/v1/backoffice/auth/me")).status_code == 401
    (token,) = await reset_tokens(session_factory, admin_user.id)
    assert token.invalidated_at is not None
    (audit,) = await audit_rows(session_factory, AuditAction.USER_PASSWORD_RESET_BY_ADMIN)
    assert audit.actor_user_id is None and audit.entity_id == str(admin_user.id)
    assert audit.details == {"source": "CLI"}

    # La contraseña anterior deja de servir; la temporal sirve y obliga a cambiarla.
    assert (await login_as(make_bo_client(), admin_user)).status_code == 401
    logged = await login_as(make_bo_client(), admin_user, password=match["password"])
    assert logged.status_code == 200 and logged.json()["mustChangePassword"] is True


async def test_reset_password_of_inactive_user_is_allowed(
    run_cli: Callable[..., object], session_factory: Sessions, operator_user: BoUser
) -> None:
    await update_user(session_factory, operator_user.id, is_active=False)
    code, out, _ = await run_cli("reset-password", "--username", operator_user.username)
    assert code == 0 and "(Operador)" in out


async def test_reset_password_unknown_or_deleted_user(
    run_cli: Callable[..., object], session_factory: Sessions, admin_user: BoUser
) -> None:
    expected = (1, "", "No existe un usuario vigente con ese nombre de usuario.\n")
    assert await run_cli("reset-password", "--username", "nadie") == expected
    await update_user(session_factory, admin_user.id, is_deleted=1, deleted_at=datetime.now(UTC))
    assert await run_cli("reset-password", "--username", admin_user.username) == expected
    assert await audit_rows(session_factory, AuditAction.USER_PASSWORD_RESET_BY_ADMIN) == []


# --- B2.15 Nada sensible en los logs ---


async def test_cli_logs_have_no_secrets(
    run_cli: Callable[..., object], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _, created, _ = await run_cli(*ADMIN_ARGS)
        _, reset, _ = await run_cli("reset-password", "--username", "admin")
        await run_cli(*ADMIN_ARGS)  # duplicado
    assert caplog.records
    text_logged = caplog.text + str([vars(r) for r in caplog.records])
    for secret in (OUTPUT.match(created)["password"], OUTPUT.match(reset)["password"], "00000000", "admin@"):
        assert secret not in text_logged


# --- purge (B2.16) ---


@pytest.fixture
async def purge_data(session_factory: Sessions) -> dict[str, list[uuid.UUID]]:
    """Filas viejas (se borran) y recientes (se conservan), con fechas simuladas."""
    user = await create_bo_user(session_factory, username="viejo.qa", dni="12121212", email="v@correo.test")
    now = datetime.now(UTC)
    days = timedelta(days=1)
    old: list[uuid.UUID] = []
    kept: list[uuid.UUID] = []
    async with session_factory.begin() as session:

        def add(row: object, *, delete: bool) -> None:
            session.add(row)
            (old if delete else kept).append(row.id)  # type: ignore[attr-defined]

        add(AuditLog(id=uuid.uuid4(), action="AUTH_LOGOUT", occurred_at=now - 366 * days), delete=True)
        add(AuditLog(id=uuid.uuid4(), action="AUTH_LOGOUT", occurred_at=now - 364 * days), delete=False)

        def user_session(**values: object) -> UserSession:
            base = {
                "id": uuid.uuid4(),
                "user_id": user.id,
                "token_hash": secrets.token_hex(32),
                "created_at": now - 2 * days,
                "last_activity_at": now - 2 * days,
                "expires_at": now + days,
            }
            return UserSession(**{**base, **values})

        add(user_session(revoked_at=now - 31 * days, revoked_reason="LOGOUT"), delete=True)
        add(user_session(created_at=now - 32 * days, expires_at=now - 31 * days, last_activity_at=now - 29 * days),
            delete=True)  # fmt: skip
        add(user_session(last_activity_at=now - 31 * days), delete=True)
        add(user_session(revoked_at=now - 29 * days, revoked_reason="LOGOUT"), delete=False)
        add(user_session(last_activity_at=now), delete=False)

        def token(created_at: datetime) -> PasswordResetToken:
            return PasswordResetToken(
                id=uuid.uuid4(),
                user_id=user.id,
                token_hash=secrets.token_hex(32),
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=30),
            )

        add(token(now - 31 * days), delete=True)
        add(token(now - 29 * days), delete=False)
    return {"old": old, "kept": kept}


async def existing_ids(sessions: Sessions) -> set[uuid.UUID]:
    async with sessions() as session:
        ids: set[uuid.UUID] = set()
        for model in (AuditLog, UserSession, PasswordResetToken):
            ids |= set(await session.scalars(select(model.id)))
        return ids


async def test_purge_deletes_only_old_rows(
    run_cli: Callable[..., object], session_factory: Sessions, purge_data: dict[str, list[uuid.UUID]]
) -> None:
    code, out, err = await run_cli("purge")

    assert (code, err) == (0, "")
    assert out == "Bitácora: 1 · Sesiones: 3 · Enlaces de restablecimiento: 1\n"
    remaining = await existing_ids(session_factory)
    assert not remaining & set(purge_data["old"])
    assert set(purge_data["kept"]) <= remaining
    (purge,) = await audit_rows(session_factory, AuditAction.SYSTEM_PURGE)
    assert purge.actor_user_id is None
    assert {k: v for k, v in purge.details.items() if not k.endswith("Cutoff")} == {
        "auditLogsDeleted": 1,
        "sessionsDeleted": 3,
        "resetTokensDeleted": 1,
    }
    audit_cutoff = datetime.fromisoformat(purge.details["auditCutoff"])
    sessions_cutoff = datetime.fromisoformat(purge.details["sessionsCutoff"])
    assert audit_cutoff.utcoffset() == timedelta(hours=-5)
    assert abs(purge.occurred_at - audit_cutoff - timedelta(days=365)) < timedelta(seconds=1)
    assert abs(purge.occurred_at - sessions_cutoff - timedelta(days=30)) < timedelta(seconds=1)


async def test_purge_dry_run_only_counts(
    run_cli: Callable[..., object], session_factory: Sessions, purge_data: dict[str, list[uuid.UUID]]
) -> None:
    before = await existing_ids(session_factory)
    code, out, _ = await run_cli("purge", "--dry-run")
    assert code == 0
    assert out == (
        "Simulación (--dry-run): no se borró nada.\n"
        "Bitácora: 1 · Sesiones: 3 · Enlaces de restablecimiento: 1\n"
    )
    assert await existing_ids(session_factory) == before
    assert await audit_rows(session_factory, AuditAction.SYSTEM_PURGE) == []


async def test_purge_with_custom_days(
    run_cli: Callable[..., object], purge_data: dict[str, list[uuid.UUID]]
) -> None:
    code, out, _ = await run_cli("purge", "--dry-run", "--audit-days", "90", "--sessions-days", "1")
    assert code == 0
    assert out.splitlines()[-1] == "Bitácora: 2 · Sesiones: 4 · Enlaces de restablecimiento: 2"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--audit-days", "89"], "--audit-days debe ser 90 o más."),
        (["--sessions-days", "0"], "--sessions-days debe ser 1 o más."),
    ],
)
async def test_purge_minimum_days(
    run_cli: Callable[..., object], session_factory: Sessions, purge_data: dict[str, list[uuid.UUID]],
    argv: list[str], message: str,
) -> None:  # fmt: skip
    before = await existing_ids(session_factory)
    assert await run_cli("purge", *argv) == (1, "", f"{message}\n")
    assert await existing_ids(session_factory) == before


@pytest.fixture
async def restricted_sessions(test_database_url: str) -> AsyncIterator[Sessions]:
    """Rol temporal con los permisos de `consent_app` tras el REVOKE de la sección 4.7 (sin UPDATE ni DELETE sobre
    audit_logs). Solo para este test; se elimina al terminar."""
    admin = create_async_engine(test_database_url, poolclass=pool.NullPool)
    role = f"bo_purge_test_{uuid.uuid4().hex[:8]}"
    password = secrets.token_hex(16)
    database = make_url(test_database_url).database
    async with admin.begin() as conn:
        if not await conn.scalar(
            text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
        ):
            await admin.dispose()
            pytest.skip("El usuario de la base de pruebas no puede crear roles")
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        await conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {role}'))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        )
        await conn.execute(text(f"REVOKE UPDATE, DELETE ON audit_logs FROM {role}"))
    url = make_url(test_database_url).set(username=role, password=password)
    restricted = create_async_engine(url, poolclass=pool.NullPool)
    try:
        yield async_sessionmaker(restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP OWNED BY {role}"))
            await conn.execute(text(f"DROP ROLE {role}"))
        await admin.dispose()


async def test_purge_requires_the_owner(
    run_cli: Callable[..., object],
    session_factory: Sessions,
    purge_data: dict[str, list[uuid.UUID]],
    restricted_sessions: Sessions,
) -> None:
    before = await existing_ids(session_factory)

    code, out, err = await run_cli("purge", sessions=restricted_sessions)

    assert (code, out) == (1, "")
    assert err == "La depuración requiere el usuario dueño de la base (consent_owner).\n"
    # Revierte todo, incluidas las sesiones que sí podía borrar.
    assert await existing_ids(session_factory) == before
    # El dry-run solo lee: funciona con el usuario de la app.
    assert (await run_cli("purge", "--dry-run", sessions=restricted_sessions))[0] == 0


# --- Punto de entrada (python -m app.cli.backoffice) ---


def test_entry_point_exit_codes() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "app.cli.backoffice", *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

    assert run("--help").returncode == 0
    invalid = run("purge", "--audit-days", "89")
    assert invalid.returncode == 1
    assert invalid.stderr == "--audit-days debe ser 90 o más.\n" and invalid.stdout == ""
