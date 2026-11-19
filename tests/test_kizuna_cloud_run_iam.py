"""Entrada de KIZUNA con Cloud Run IAM (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, fase I1).

API_SURFACE, KIZUNA_INBOUND_AUTH, el chequeo al arrancar, el archivo de variables del servicio privado, Postman y el
retiro de Apigee. Cloud Run IAM en sí no se puede probar aquí: se valida en Producción (criterios I2).
"""

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.surface import include_routers, routers_for
from app.api.v1 import backoffice, health, kizuna, public, webhooks
from app.core.config import Settings, get_settings
from app.core.security import ApiKeyDep, InsecureConfigurationError, check_security_settings
from scripts import kizuna_env

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"
KIZUNA_PATHS = {"/api/v1/consents", "/api/v1/consents/{IdentifierId}"}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"database_url": DB_URL, "api_key_enabled": True, "api_key": "una-clave"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def route_paths(app: FastAPI) -> set[str]:
    # FastAPI 0.162 guarda los routers incluidos como _IncludedRouter (app.routes no los aplana): el OpenAPI sí lista
    # todas las rutas efectivas (todas las de la API están en el esquema).
    return set(app.openapi()["paths"])


def app_for(settings: Settings) -> FastAPI:
    app = FastAPI()
    include_routers(app, settings)
    return app


# --- Variables ---


def test_defaults_keep_todays_behavior() -> None:
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert settings.api_surface == "all"
    assert settings.kizuna_inbound_auth == "api_key"


def test_values_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_SURFACE", " KIZUNA ")
    monkeypatch.setenv("KIZUNA_INBOUND_AUTH", "Cloud_Run_IAM")
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert (settings.api_surface, settings.kizuna_inbound_auth) == ("kizuna", "cloud_run_iam")


@pytest.mark.parametrize(("field", "value"), [("api_surface", "privado"), ("kizuna_inbound_auth", "iam")])
def test_unknown_values_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


# --- API_SURFACE: routers montados (criterio I1.2) ---


def test_routers_per_surface() -> None:
    every = [health.router, kizuna.router, public.router, webhooks.router, backoffice.router]
    assert routers_for(make_settings(api_surface="all")) == every
    assert routers_for(make_settings(api_surface="public")) == [
        health.router,
        public.router,
        webhooks.router,
        backoffice.router,
    ]
    assert routers_for(make_settings(api_surface="kizuna")) == [health.router, kizuna.router]


def test_all_mounts_the_same_routes_as_the_main_app() -> None:
    from app.main import app

    assert route_paths(app_for(make_settings(api_surface="all"))) == route_paths(app)


def test_public_surface_has_no_kizuna_routes() -> None:
    paths = route_paths(app_for(make_settings(api_surface="public")))
    assert not paths & KIZUNA_PATHS
    assert "/health" in paths
    assert any(path.startswith("/api/v1/public/") for path in paths)
    assert any(path.startswith("/api/v1/webhooks/") for path in paths)
    assert any(path.startswith("/api/v1/backoffice/") for path in paths)


def test_kizuna_surface_only_has_kizuna_routes_and_health() -> None:
    assert route_paths(app_for(make_settings(api_surface="kizuna"))) == KIZUNA_PATHS | {"/health"}


# --- Chequeo al arrancar (criterio I1.3) ---


@pytest.mark.parametrize("surface", ["all", "public"])
def test_cloud_run_iam_outside_the_kizuna_surface_does_not_start(
    caplog: pytest.LogCaptureFixture, surface: str
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="app.core.security"),
        pytest.raises(InsecureConfigurationError, match="solo se admite con API_SURFACE=kizuna"),
    ):
        check_security_settings(make_settings(api_surface=surface, kizuna_inbound_auth="cloud_run_iam"))
    (record,) = caplog.records
    assert record.levelname == "CRITICAL"


def test_cloud_run_iam_on_the_kizuna_surface_starts_without_api_key(caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(api_surface="kizuna", kizuna_inbound_auth="cloud_run_iam", api_key="")
    with caplog.at_level(logging.INFO, logger="app.core.security"):
        check_security_settings(settings)
    (record,) = caplog.records
    assert record.levelname == "INFO"
    assert "debe ser privado" in record.getMessage()


def test_public_surface_does_not_need_the_api_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.security"):
        check_security_settings(make_settings(api_surface="public", api_key_enabled=True, api_key=""))
    assert caplog.records == []


@pytest.mark.parametrize("surface", ["all", "kizuna"])
def test_api_key_rules_still_apply_where_kizuna_uses_it(surface: str) -> None:
    with pytest.raises(InsecureConfigurationError, match="API_KEY está vacía"):
        check_security_settings(make_settings(api_surface=surface, api_key=""))


def _run_python(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Importa app.main en otro proceso (lo que hace Cloud Run al arrancar); las variables pisan al .env."""
    return subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": DB_URL, "PYTHONIOENCODING": "utf-8", **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


PRINT_PATHS = "import json; from app.main import app; print(json.dumps(sorted(app.openapi()['paths'])))"


def test_private_kizuna_service_starts_without_api_key() -> None:
    result = _run_python(
        PRINT_PATHS,
        API_SURFACE="kizuna",
        KIZUNA_INBOUND_AUTH="cloud_run_iam",
        API_KEY_ENABLED="true",
        API_KEY="",
        DOCS_ENABLED="false",
    )
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout.strip().splitlines()[-1])) == KIZUNA_PATHS | {"/health"}


def test_public_service_starts_without_api_key_and_without_kizuna_routes() -> None:
    result = _run_python(
        PRINT_PATHS, API_SURFACE="public", API_KEY_ENABLED="true", API_KEY="", DOCS_ENABLED="false"
    )
    assert result.returncode == 0, result.stderr
    assert not set(json.loads(result.stdout.strip().splitlines()[-1])) & KIZUNA_PATHS


def test_main_app_does_not_start_with_cloud_run_iam_and_every_route() -> None:
    result = _run_python("import app.main", API_SURFACE="all", KIZUNA_INBOUND_AUTH="cloud_run_iam")
    assert result.returncode != 0
    assert "InsecureConfigurationError" in result.stderr


# --- KIZUNA_INBOUND_AUTH en la dependencia (criterio I1.4) ---


def protected_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings
    router = APIRouter(dependencies=[ApiKeyDep])

    @router.get("/protegido")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


async def get_status(app: FastAPI, headers: dict[str, str] | None = None) -> int:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return (await client.get("/protegido", headers=headers)).status_code


async def test_cloud_run_iam_skips_the_api_key_only_on_the_kizuna_surface() -> None:
    private = make_settings(api_surface="kizuna", kizuna_inbound_auth="cloud_run_iam", api_key="")
    assert await get_status(protected_app(private)) == 200
    # Defensa: aunque esa combinación no arranca, la dependencia no se salta la API Key fuera del servicio privado.
    misconfigured = make_settings(api_surface="all", kizuna_inbound_auth="cloud_run_iam")
    assert await get_status(protected_app(misconfigured)) == 401


async def test_api_key_mode_is_unchanged() -> None:
    settings = make_settings(api_surface="kizuna", kizuna_inbound_auth="api_key")
    assert await get_status(protected_app(settings)) == 401
    assert await get_status(protected_app(settings), {"X-API-Key": "una-clave"}) == 200


@pytest.fixture
async def private_kizuna_client(
    session_factory: async_sessionmaker[AsyncSession], test_settings: Settings, whatsapp_sender: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """La API como el servicio consent-api-kizuna (sin header X-API-Key), contra la base de pruebas."""
    from app.db.session import get_session, get_session_factory
    from app.integrations.whatsapp import get_whatsapp_sender

    settings = test_settings.model_copy(
        update={"api_surface": "kizuna", "kizuna_inbound_auth": "cloud_run_iam"}
    )
    app = app_for(settings)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        get_session: override_session,
        get_session_factory: lambda: session_factory,
        get_settings: lambda: settings,
        get_whatsapp_sender: lambda: whatsapp_sender,
    }
    app.dependency_overrides.update(overrides)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_private_service_registers_and_queries_without_api_key(
    private_kizuna_client: httpx.AsyncClient,
) -> None:
    body = {
        "_request": {
            "IdentifierId": "TRX-IAM-001",
            "customerId": "12345678",
            "customerName": "Juan Perez",
            "customerPhone": "987654321",
            "dealerCode": "DEALER-001",
        }
    }
    created = await private_kizuna_client.post("/api/v1/consents", json=body)
    assert created.status_code == 201, created.text
    detail = await private_kizuna_client.get("/api/v1/consents/TRX-IAM-001")
    assert detail.status_code == 200
    assert detail.json()["IdentifierId"] == "TRX-IAM-001"
    # Lo que no es de KIZUNA no está montado en el servicio privado.
    assert (await private_kizuna_client.get("/api/v1/backoffice/auth/me")).status_code == 404
    assert (await private_kizuna_client.get("/api/v1/public/consent-links/x")).status_code == 404


# --- Variables del servicio privado: scripts/kizuna_env.py (criterio I1.5) ---


def parse_env(text: str) -> dict[str, str]:
    return dict(re.findall(r'^([A-Z][A-Z0-9_]*):\s*"(.*?)"', text, re.MULTILINE))


def test_generated_env_only_changes_the_two_keys() -> None:
    source = (PROJECT_ROOT / "deploy" / "cloudrun.env.yaml").read_text(encoding="utf-8")
    generated = kizuna_env.kizuna_env_text(source)
    base, private = parse_env(source), parse_env(generated)
    assert private.keys() == base.keys()
    assert {key for key in base if base[key] != private[key]} == {"API_SURFACE", "KIZUNA_INBOUND_AUTH"}
    assert private["API_SURFACE"] == "kizuna"
    assert private["KIZUNA_INBOUND_AUTH"] == "cloud_run_iam"
    assert generated.startswith("# GENERADO por scripts/kizuna_env.py")
    # Las variables generadas arrancan: API_KEY vacía y cloud_run_iam en la superficie kizuna.
    check_security_settings(
        make_settings(api_surface="kizuna", kizuna_inbound_auth="cloud_run_iam", api_key="")
    )


@pytest.mark.parametrize(
    "source",
    [
        'API_SURFACE: "public"\n',
        'KIZUNA_INBOUND_AUTH: "api_key"\n',
        'API_SURFACE: "public"\nAPI_SURFACE: "all"\nKIZUNA_INBOUND_AUTH: "api_key"\n',
    ],
)
def test_generation_fails_unless_each_key_appears_once(source: str) -> None:
    with pytest.raises(ValueError, match="exactamente una vez"):
        kizuna_env.kizuna_env_text(source)


def test_cli_writes_the_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "base.yaml"
    source.write_text(
        'APP_ENV: "production"\nAPI_SURFACE: "public"   # comentario\nKIZUNA_INBOUND_AUTH: "api_key"\n'
    )
    output = tmp_path / "kizuna.yaml"
    monkeypatch.setattr("sys.argv", ["kizuna_env", "--source", str(source), "--output", str(output)])
    kizuna_env.main()
    assert parse_env(output.read_text(encoding="utf-8")) == {
        "APP_ENV": "production",
        "API_SURFACE": "kizuna",
        "KIZUNA_INBOUND_AUTH": "cloud_run_iam",
    }
    assert "\r\n" not in output.read_bytes().decode("utf-8")
    assert "consent-api-kizuna" in capsys.readouterr().out


def test_generated_file_is_ignored_by_git() -> None:
    assert (
        "deploy/cloudrun-kizuna.env.yaml"
        in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )


# --- Postman (criterio I1.8) ---


def load_collection() -> dict[str, Any]:
    return json.loads((PROJECT_ROOT / "postman" / "collection.json").read_text(encoding="utf-8"))


def test_postman_folder_covers_the_kizuna_operations_with_the_id_token() -> None:
    collection = load_collection()
    (folder,) = [item for item in collection["item"] if item["name"] == "KIZUNA vía Cloud Run IAM"]
    assert folder["auth"] == {
        "type": "bearer",
        "bearer": [{"key": "token", "value": "{{kizuna_id_token}}", "type": "string"}],
    }
    operations = set()
    for request in (item["request"] for item in folder["item"]):
        raw = request["url"]["raw"]
        assert raw.startswith("{{kizuna_api_url}}/api/v1/")
        path = raw.removeprefix("{{kizuna_api_url}}").split("?")[0]
        operations.add((request["method"], re.sub(r"\{\{transaction_id\}\}", "{IdentifierId}", path)))
    assert {
        ("POST", "/api/v1/consents"),
        ("GET", "/api/v1/consents"),
        ("GET", "/api/v1/consents/{IdentifierId}"),
    } <= operations
    (no_token,) = [item for item in folder["item"] if item["name"] == "Sin token (403)"]
    assert no_token["request"]["auth"] == {"type": "noauth"}
    variables = {variable["key"]: variable["value"] for variable in collection["variable"]}
    assert variables["kizuna_api_url"] == "https://<URL_CONSENT_API_KIZUNA>"
    assert variables["kizuna_id_token"] == ""


# --- Apigee retirado de esta rama (criterio I1.9) ---


def test_no_apigee_left_in_code_deploy_scripts_or_postman() -> None:
    suffixes = {".py", ".json", ".yaml", ".yml", ".xml", ".md", ".txt"}
    files = [
        path
        for folder in ("app", "deploy", "scripts", "postman", "mocks")
        for path in (PROJECT_ROOT / folder).rglob("*")
        if path.is_file() and path.suffix in suffixes
    ]
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in files
        if "apigee" in path.read_text(encoding="utf-8").lower()
    ]
    for name in (".env.example", "docker-compose.yml"):
        if "apigee" in (PROJECT_ROOT / name).read_text(encoding="utf-8").lower():
            offenders.append(name)
    assert offenders == []
    assert not (PROJECT_ROOT / "deploy" / "apigee").exists()
