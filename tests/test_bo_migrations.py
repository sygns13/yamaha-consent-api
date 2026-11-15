"""Criterio B1.1: migraciones del BackOffice en una base vacía temporal (no toca la base de desarrollo ni la de tests).

`alembic upgrade head` crea 18 tablas + alembic_version; `alembic downgrade b7c2d9e41f60` deja las 7 tablas de v1.4
con sus datos intactos (aunque haya datos del BackOffice, incluidos dealers); `alembic downgrade base` deja solo
alembic_version. La base `<DATABASE_URL>_bo_migrations` se crea al empezar y se elimina al terminar.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy import make_url, pool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command
from app.core.config import get_settings
from app.models import (
    Consent,
    ConsentEvidence,
    ConsentLink,
    ConsentRequest,
    ConsentStatus,
    KizunaNotification,
    WhatsappMessage,
    WhatsappMessageEvent,
    WhatsappStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_SUFFIX = "_bo_migrations"
V14_HEAD = "b7c2d9e41f60"
V14_TABLES = {
    "consents",
    "consent_requests",
    "consent_links",
    "consent_evidence",
    "whatsapp_messages",
    "whatsapp_message_events",
    "kizuna_notifications",
}
BACKOFFICE_TABLES = {
    "roles",
    "backoffice_modules",
    "role_modules",
    "users",
    "user_sessions",
    "password_reset_tokens",
    "ubigeo_departments",
    "ubigeo_provinces",
    "ubigeo_districts",
    "dealers",
    "audit_logs",
}


def _dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def _recreate(admin_dsn: str, name: str, *, create: bool) -> None:
    conn = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        if create:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture
def empty_database_url() -> Iterator[str]:
    url = make_url(get_settings().database_url)
    name = f"{url.database}{DB_SUFFIX}"
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    try:
        asyncio.run(_recreate(admin_dsn, name, create=True))
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL no disponible para los tests: {exc}")
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_recreate(admin_dsn, name, create=False))


def _alembic(url: str, action: str, revision: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    getattr(command, action)(config, revision)


async def _fetch(url: str, query: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_dsn(url), timeout=10)
    try:
        return await conn.fetch(query)
    finally:
        await conn.close()


def _tables(url: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        )
    )
    return {row["table_name"] for row in rows}


def _snapshot(url: str) -> dict[str, list[str]]:
    """Contenido completo de las 7 tablas de v1.4 (cada fila como JSON), para comparar antes y después."""
    return {
        table: sorted(
            row["data"]
            for row in asyncio.run(_fetch(url, f"SELECT to_jsonb(t)::text AS data FROM {table} t"))
        )
        for table in sorted(V14_TABLES)
    }


async def _insert_v14_data(url: str) -> None:
    """Una fila en cada tabla de v1.4, en el esquema de b7c2d9e41f60."""
    engine = create_async_engine(url, poolclass=pool.NullPool)
    now = datetime.now(UTC)
    try:
        async with AsyncSession(engine) as session, session.begin():
            consent = Consent(
                transaction_id="TRX-B1-1",
                customer_id="12345678",
                dealer_code="CL0001",
                dealer_name="ABC Motors",
                full_name="JUAN PÉREZ",
                phone="987654321",
                email="juan@mail.com",
                requested_at=now,
                status=ConsentStatus.ACEPTADO,
                consent_personal_data=True,
                consent_marketing_yamaha=True,
                consent_marketing_dealer=False,
                responded_at=now,
            )
            link = ConsentLink(
                transaction_id="TRX-B1-1",
                token_hash="a" * 64,
                expires_at=now + timedelta(days=7),
                used_at=now,
            )
            session.add_all([consent, link])
            await session.flush()
            message = WhatsappMessage(
                transaction_id="TRX-B1-1",
                link_id=link.id,
                twilio_message_sid=f"SM{uuid.uuid4().hex}",
                to_phone="whatsapp:+51987654321",
                content_sid="HXtest",
                status=WhatsappStatus.READ,
            )
            session.add(message)
            await session.flush()
            session.add_all(
                [
                    ConsentRequest(
                        transaction_id="TRX-B1-1", consent_transaction_id="TRX-B1-1", action="CREATED"
                    ),
                    ConsentEvidence(
                        transaction_id="TRX-B1-1",
                        link_id=link.id,
                        ip_address="200.48.1.1",
                        user_agent="pytest",
                        policy_version="v1",
                        policy_url="https://example.com/politica",
                        answers={
                            "personalDataConsent": True,
                            "marketingConsentYMDP": True,
                            "marketingConsentDEALER": None,
                        },
                        submitted_at=now,
                    ),
                    WhatsappMessageEvent(
                        message_id=message.id,
                        status=WhatsappStatus.READ,
                        raw_payload={"MessageStatus": "read"},
                    ),
                    KizunaNotification(
                        transaction_id="TRX-B1-1",
                        payload={"IdentifierId": "TRX-B1-1"},
                        target_url="http://kizuna-mock:8081/kizuna/consent-result",
                    ),
                ]
            )
    finally:
        await engine.dispose()


async def _insert_backoffice_data(url: str) -> None:
    """Un usuario, un dealer (referencia un distrito) y una fila de bitácora."""
    conn = await asyncpg.connect(_dsn(url), timeout=10)
    try:
        user_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO users (id, role_code, dni, first_names, last_names, username, email, password_hash) "
            "VALUES ($1, 'ADMIN', '00000000', 'Administrador', 'General', 'admin', 'admin@backoffice.local', 'x')",
            user_id,
        )
        await conn.execute(
            "INSERT INTO dealers (id, dealer_code, ruc, address, district_ubigeo, contact_email, created_by) "
            "VALUES ($1, 'CL0001', '20100047218', 'Av. Ejemplo 123', '150101', 'contacto@abc.pe', $2)",
            uuid.uuid4(),
            user_id,
        )
        await conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action) VALUES ($1, $2, 'DEALER_CREATED')",
            uuid.uuid4(),
            user_id,
        )
    finally:
        await conn.close()


def _scalar(url: str, query: str) -> object:
    return asyncio.run(_fetch(url, query))[0][0]


def test_backoffice_migrations_upgrade_and_downgrade(empty_database_url: str) -> None:
    url = empty_database_url

    # Base en v1.4 con datos.
    _alembic(url, "upgrade", V14_HEAD)
    assert _tables(url) == V14_TABLES | {"alembic_version"}
    asyncio.run(_insert_v14_data(url))
    before = _snapshot(url)
    assert all(len(rows) == 1 for rows in before.values())

    # upgrade head: 18 tablas + alembic_version, con semillas y Ubigeo.
    _alembic(url, "upgrade", "head")
    tables = _tables(url)
    assert tables == V14_TABLES | BACKOFFICE_TABLES | {"alembic_version"}
    assert len(tables - {"alembic_version"}) == 18
    assert _scalar(url, "SELECT count(*) FROM role_modules") == 4
    assert _scalar(url, "SELECT count(*) FROM ubigeo_districts") == 1892
    assert _snapshot(url) == before
    asyncio.run(_insert_backoffice_data(url))

    # downgrade a v1.4: solo quedan las 7 tablas, con sus datos intactos (aunque hubiera dealers).
    _alembic(url, "downgrade", V14_HEAD)
    assert _tables(url) == V14_TABLES | {"alembic_version"}
    assert _scalar(url, "SELECT version_num FROM alembic_version") == V14_HEAD
    assert _snapshot(url) == before

    # downgrade base: solo alembic_version, sin tipos enum.
    _alembic(url, "downgrade", "base")
    assert _tables(url) == {"alembic_version"}
    enums = asyncio.run(
        _fetch(
            url,
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE t.typtype = 'e' AND n.nspname = 'public'",
        )
    )
    assert enums == []


def test_seed_downgrade_alone_empties_ubigeo_and_dealers(empty_database_url: str) -> None:
    # Decisión del 15/11/2026: el downgrade de ubigeo_inei_seed usa TRUNCATE ... CASCADE (vacía también dealers).
    url = empty_database_url
    _alembic(url, "upgrade", "head")
    asyncio.run(_insert_backoffice_data(url))

    _alembic(url, "downgrade", "-1")
    assert _scalar(url, "SELECT count(*) FROM ubigeo_districts") == 0
    assert _scalar(url, "SELECT count(*) FROM ubigeo_departments") == 0
    assert _scalar(url, "SELECT count(*) FROM dealers") == 0
    assert _scalar(url, "SELECT count(*) FROM users") == 1
    assert _scalar(url, "SELECT count(*) FROM audit_logs") == 1

    # Volver a subir recarga el catálogo.
    _alembic(url, "upgrade", "head")
    assert _scalar(url, "SELECT count(*) FROM ubigeo_districts") == 1892
