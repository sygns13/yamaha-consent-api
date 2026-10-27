"""Fixtures compartidas para tests contra PostgreSQL.

Los tests de API usan una base aparte, `<DATABASE_URL>_test` (p. ej. consents_db_test), que se crea si no existe y
se migra con Alembic una vez por sesión. Cada test empieza con las tablas vacías. Si PostgreSQL no responde, los
tests que usan estas fixtures se saltan.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from alembic.config import Config
from sqlalchemy import make_url, pool, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.integrations.whatsapp import WhatsAppSendResult, get_whatsapp_sender
from app.models import WhatsappStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_SUFFIX = "_test"
TEST_API_KEY = "test-api-key"


def _test_database_url() -> str:
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}{TEST_DB_SUFFIX}").render_as_string(hide_password=False)


async def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    admin_dsn = parsed.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    conn = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", parsed.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _test_database_url()
    assert make_url(url).database.endswith(TEST_DB_SUFFIX)  # nunca vaciar la base de desarrollo
    try:
        asyncio.run(_create_database_if_missing(url))
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL no disponible para los tests: {exc}")

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def db_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url, poolclass=pool.NullPool, hide_parameters=True)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def test_settings(test_database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=test_database_url,
        api_key_enabled=True,
        api_key=TEST_API_KEY,
        link_ttl_hours=168,
        phone_country_prefix="51",
        twilio_content_sid="HXtest",
    )


class RecordingWhatsAppSender:
    """WhatsAppSender de prueba: guarda cada envío y devuelve `result` (o lanza `error`)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.result = WhatsAppSendResult(status=WhatsappStatus.QUEUED, message_sid=None)
        self.error: Exception | None = None

    async def send_consent_link(self, *, to: str, token: str, transaction_id: str) -> WhatsAppSendResult:
        self.calls.append({"to": to, "token": token, "transaction_id": transaction_id})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def whatsapp_sender() -> RecordingWhatsAppSender:
    return RecordingWhatsAppSender()


@pytest.fixture
async def api_client(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    whatsapp_sender: RecordingWhatsAppSender,
) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente HTTP contra la app real, con la base de pruebas, la configuración de prueba y el sender de prueba.

    Cada petición usa su propia sesión (y conexión): las peticiones concurrentes son concurrentes en la base.
    """
    from app.db.session import get_session
    from app.main import app

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        get_session: override_session,
        get_settings: lambda: test_settings,
        get_whatsapp_sender: lambda: whatsapp_sender,
    }
    app.dependency_overrides.update(overrides)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": TEST_API_KEY}
        ) as client:
            yield client
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)
