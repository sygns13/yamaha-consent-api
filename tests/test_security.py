import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from app.core.config import Settings, get_settings
from app.core.security import (
    INVALID_API_KEY,
    MISSING_API_KEY,
    ApiKeyDep,
    InsecureConfigurationError,
    add_cors,
    check_security_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"
API_KEY = "clave-super-secreta-123"
LANDING = "https://consentimiento.dominio.com"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": DB_URL,
        "api_key_enabled": True,
        "api_key_header": "X-API-Key",
        "api_key": API_KEY,
        "cors_allowed_origins": [LANDING],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_app(settings: Settings) -> FastAPI:
    """App de prueba: un router protegido (como el de KIZUNA) y una ruta pública."""
    app = FastAPI()
    add_cors(app, settings)
    app.dependency_overrides[get_settings] = lambda: settings

    kizuna = APIRouter(prefix="/api/v1", dependencies=[ApiKeyDep])

    @kizuna.get("/consents/{transaction_id}")
    async def get_consent(transaction_id: str) -> dict[str, str]:
        return {"transaction_id": transaction_id}

    @app.get("/api/v1/public/ping")
    async def public() -> dict[str, str]:
        return {"ok": "sí"}

    app.include_router(kizuna)
    return app


async def call(settings: Settings, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


PROTECTED = "/api/v1/consents/TRX-1"


# --- API Key ---


async def test_valid_api_key() -> None:
    response = await call(make_settings(), "GET", PROTECTED, headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    assert response.json() == {"transaction_id": "TRX-1"}


@pytest.mark.parametrize(
    "value",
    [
        "otra-clave",
        API_KEY + "x",
        API_KEY[:-1],
        API_KEY.upper(),
        f" {API_KEY}",
    ],
)
async def test_invalid_api_key(value: str) -> None:
    response = await call(make_settings(), "GET", PROTECTED, headers={"X-API-Key": value})
    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_API_KEY}


async def test_missing_api_key() -> None:
    response = await call(make_settings(), "GET", PROTECTED)
    assert response.status_code == 401
    assert response.json() == {"detail": MISSING_API_KEY}


async def test_empty_api_key_header() -> None:
    response = await call(make_settings(), "GET", PROTECTED, headers={"X-API-Key": ""})
    assert response.status_code == 401


async def test_header_name_is_case_insensitive() -> None:
    response = await call(make_settings(), "GET", PROTECTED, headers={"x-api-key": API_KEY})
    assert response.status_code == 200


async def test_non_ascii_api_key_is_rejected_not_500() -> None:
    # compare_digest con str no ASCII lanzaría TypeError: se compara en bytes.
    response = await call(
        make_settings(), "GET", PROTECTED, headers={"X-API-Key": "clave-ñ".encode("latin-1")}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_API_KEY}


async def test_validation_disabled_allows_requests_without_key() -> None:
    settings = make_settings(api_key_enabled=False)
    assert (await call(settings, "GET", PROTECTED)).status_code == 200
    assert (await call(settings, "GET", PROTECTED, headers={"X-API-Key": "cualquiera"})).status_code == 200


async def test_custom_header_name() -> None:
    settings = make_settings(api_key_header="X-Kizuna-Key")
    assert (await call(settings, "GET", PROTECTED, headers={"X-Kizuna-Key": API_KEY})).status_code == 200
    # El header por defecto ya no sirve.
    response = await call(settings, "GET", PROTECTED, headers={"X-API-Key": API_KEY})
    assert response.status_code == 401
    assert response.json() == {"detail": MISSING_API_KEY}


async def test_dependency_without_configured_key_rejects_everything() -> None:
    # La app real no arranca así (check_security_settings); la dependencia igual falla cerrado.
    settings = make_settings(api_key="")
    assert (await call(settings, "GET", PROTECTED)).status_code == 401
    assert (await call(settings, "GET", PROTECTED, headers={"X-API-Key": "x"})).status_code == 401


async def test_public_routes_do_not_require_api_key() -> None:
    assert (await call(make_settings(), "GET", "/api/v1/public/ping")).status_code == 200


async def test_rejection_log_never_contains_the_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.security"):
        await call(make_settings(), "GET", PROTECTED, headers={"X-API-Key": "clave-enviada-por-error"})

    assert "clave-enviada-por-error" not in caplog.text
    assert API_KEY not in caplog.text
    record = next(r for r in caplog.records if r.name == "app.core.security")
    assert record.path == "/api/v1/consents/{transaction_id}"


def test_openapi_documents_the_api_key() -> None:
    app = make_app(make_settings())
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["ApiKey"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "API Key de KIZUNA",
    }
    assert schema["paths"][PROTECTED.replace("TRX-1", "{transaction_id}")]["get"]["security"] == [
        {"ApiKey": []}
    ]
    assert "security" not in schema["paths"]["/api/v1/public/ping"]["get"]


# --- Chequeo al arrancar ---


def test_startup_warning_when_validation_disabled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.security"):
        check_security_settings(make_settings(api_key_enabled=False, api_key=""))
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert "API_KEY_ENABLED=false" in record.getMessage()


@pytest.mark.parametrize("api_key", ["", "   "])
def test_startup_fails_when_enabled_without_key(caplog: pytest.LogCaptureFixture, api_key: str) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="app.core.security"),
        pytest.raises(InsecureConfigurationError, match="API_KEY está vacía"),
    ):
        check_security_settings(make_settings(api_key=api_key))
    (record,) = caplog.records
    assert record.levelname == "CRITICAL"


def test_no_startup_log_when_correctly_configured(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.security"):
        check_security_settings(make_settings())
    assert caplog.records == []


def _run(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    """Ejecuta un proceso aparte con la configuración dada (las variables de entorno pisan al .env)."""
    return subprocess.run(
        [sys.executable, *args],
        check=False,
        cwd=PROJECT_ROOT,
        # PYTHONIOENCODING: en Windows el traceback de stderr saldría en cp1252.
        env={**os.environ, "DATABASE_URL": DB_URL, "PYTHONIOENCODING": "utf-8", **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_uvicorn_does_not_start_when_enabled_without_key() -> None:
    # Lo mismo que pasa en Cloud Run: el proceso termina con error y la revisión no queda activa.
    result = _run(
        ["-m", "uvicorn", "app.main:app", "--port", "0"],
        API_KEY_ENABLED="true",
        API_KEY="",
    )
    assert result.returncode != 0
    assert "InsecureConfigurationError" in result.stderr + result.stdout
    assert '"severity": "CRITICAL"' in result.stdout


def test_app_loads_when_enabled_with_key() -> None:
    result = _run(["-c", "import app.main"], API_KEY_ENABLED="true", API_KEY="una-clave")
    assert result.returncode == 0, result.stderr


# --- CORS ---


def preflight_headers(origin: str, method: str = "POST", headers: str = "content-type") -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    }


async def test_cors_preflight_from_landing_is_allowed() -> None:
    response = await call(
        make_settings(), "OPTIONS", "/api/v1/public/ping", headers=preflight_headers(LANDING)
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LANDING
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in response.headers


async def test_cors_preflight_from_other_origin_is_rejected() -> None:
    response = await call(
        make_settings(), "OPTIONS", "/api/v1/public/ping", headers=preflight_headers("https://malicioso.com")
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_cors_simple_request_headers() -> None:
    allowed = await call(make_settings(), "GET", "/api/v1/public/ping", headers={"Origin": LANDING})
    assert allowed.headers["access-control-allow-origin"] == LANDING

    other = await call(
        make_settings(), "GET", "/api/v1/public/ping", headers={"Origin": "https://malicioso.com"}
    )
    assert "access-control-allow-origin" not in other.headers


@pytest.mark.parametrize(("method", "headers"), [("DELETE", "content-type"), ("POST", "x-api-key")])
async def test_cors_only_allows_what_the_landing_needs(method: str, headers: str) -> None:
    response = await call(
        make_settings(), "OPTIONS", "/api/v1/public/ping", headers=preflight_headers(LANDING, method, headers)
    )
    assert response.status_code == 400


async def test_cors_with_several_origins() -> None:
    settings = make_settings(cors_allowed_origins="https://a.com, https://b.com")
    for origin in ("https://a.com", "https://b.com"):
        response = await call(settings, "GET", "/api/v1/public/ping", headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin


async def test_cors_without_origins_allows_none() -> None:
    response = await call(
        make_settings(cors_allowed_origins=[]), "GET", "/api/v1/public/ping", headers={"Origin": LANDING}
    )
    assert "access-control-allow-origin" not in response.headers


def test_main_app_has_cors_and_request_id_in_order() -> None:
    from starlette.middleware.cors import CORSMiddleware

    from app.core.middleware import RequestIdMiddleware
    from app.main import app

    classes = [m.cls for m in app.user_middleware]
    # user_middleware[0] es el más externo.
    assert classes == [RequestIdMiddleware, CORSMiddleware]
