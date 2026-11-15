"""Chequeo de configuración del BackOffice al arrancar (BACKOFFICE_DEALERS.spec.md, sección 5.12; criterio B0.4)."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.backoffice_security import check_backoffice_settings
from app.core.config import Settings
from app.core.security import InsecureConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, database_url=DB_URL, **overrides)


def test_production_without_secure_cookie_fails(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="app.core.backoffice_security"),
        pytest.raises(InsecureConfigurationError, match="BACKOFFICE_SESSION_COOKIE_SECURE=false"),
    ):
        check_backoffice_settings(make_settings(app_env="production", backoffice_session_cookie_secure=False))
    (record,) = caplog.records
    assert record.levelname == "CRITICAL"


@pytest.mark.parametrize(
    ("app_env", "secure"),
    [("production", True), ("local", False), ("local", True)],
)
def test_other_combinations_start_without_logs(
    caplog: pytest.LogCaptureFixture, app_env: str, secure: bool
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.backoffice_security"):
        check_backoffice_settings(make_settings(app_env=app_env, backoffice_session_cookie_secure=secure))
    assert caplog.records == []


def _run(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    """Proceso aparte en Producción, con el resto de los chequeos de arranque satisfechos."""
    return subprocess.run(
        [sys.executable, *args],
        check=False,
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            # PYTHONIOENCODING: en Windows el traceback de stderr saldría en cp1252.
            "PYTHONIOENCODING": "utf-8",
            "DATABASE_URL": DB_URL,
            "APP_ENV": "production",
            "API_KEY_ENABLED": "true",
            "API_KEY": "una-clave",
            "TWILIO_SEND_ENABLED": "false",
            "KIZUNA_CALLBACK_ENABLED": "false",
            "EMAIL_SEND_ENABLED": "false",
            **env,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_uvicorn_does_not_start_in_production_without_secure_cookie() -> None:
    # Lo mismo que pasa en Cloud Run: el proceso termina con error y la revisión no queda activa.
    result = _run(["-m", "uvicorn", "app.main:app", "--port", "0"], BACKOFFICE_SESSION_COOKIE_SECURE="false")
    assert result.returncode != 0
    assert "InsecureConfigurationError" in result.stderr + result.stdout
    assert '"severity": "CRITICAL"' in result.stdout


def test_app_loads_in_production_with_secure_cookie() -> None:
    result = _run(["-c", "import app.main"], BACKOFFICE_SESSION_COOKIE_SECURE="true")
    assert result.returncode == 0, result.stderr
