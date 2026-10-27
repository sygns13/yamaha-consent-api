"""deploy/cloudrun.env.yaml debe declarar todas las variables del backend salvo los secretos (M7)."""

import re
from pathlib import Path

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Se montan desde Secret Manager con --set-secrets (sección M7 de la spec).
SECRETS = {"DATABASE_URL", "API_KEY", "TWILIO_AUTH_TOKEN", "KIZUNA_CALLBACK_AUTH_VALUE"}


def cloudrun_env() -> dict[str, str]:
    text = (PROJECT_ROOT / "deploy" / "cloudrun.env.yaml").read_text(encoding="utf-8")
    return dict(re.findall(r'^([A-Z][A-Z0-9_]*):\s*"(.*?)"', text, re.MULTILINE))


def test_template_declares_every_non_secret_setting() -> None:
    settings = {name.upper() for name in Settings.model_fields}
    assert sorted(settings - SECRETS - cloudrun_env().keys()) == []
    assert sorted(cloudrun_env().keys() - settings) == []


def test_template_has_no_secrets() -> None:
    assert not SECRETS & cloudrun_env().keys()


def test_template_production_defaults() -> None:
    env = cloudrun_env()
    assert env["APP_ENV"] == "production"
    assert env["API_KEY_ENABLED"] == "true"
    assert env["TWILIO_SIGNATURE_VALIDATION_ENABLED"] == "true"
    # M7, paso 5: apagado hasta tener el endpoint real de KIZUNA.
    assert env["KIZUNA_CALLBACK_ENABLED"] == "false"


def test_every_setting_is_in_env_example() -> None:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))
    assert sorted({name.upper() for name in Settings.model_fields} - declared) == []
