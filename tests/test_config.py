from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"


def test_env_example_lists_every_setting() -> None:
    keys = set(dotenv_values(ENV_EXAMPLE))
    expected = {name.upper() for name in Settings.model_fields}
    assert expected - keys == set(), "faltan en .env.example"
    assert keys - expected == set(), "sobran en .env.example (no existen en Settings)"


def test_env_example_parses() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)
    # Los comentarios en línea no deben quedar dentro del valor.
    assert settings.api_key.get_secret_value() == ""
    assert settings.cors_allowed_origins == ["http://localhost:4321"]
    assert settings.kizuna_callback_backoff_seconds == [5, 30, 120]
    assert settings.twilio_signature_validation_enabled is True
    # BackOffice (sección 9.1 del spec del BackOffice): valores locales.
    assert settings.backoffice_base_url == "http://localhost:5173"
    assert settings.backoffice_session_cookie_secure is False
    assert settings.email_send_enabled is True
    assert settings.email_from_name == "BackOffice Consentimientos Yamaha"
    assert (settings.smtp_host, settings.smtp_port, settings.smtp_security) == ("localhost", 1025, "none")
    assert settings.smtp_password.get_secret_value() == ""


def test_defaults_from_section_9() -> None:
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert settings.app_env == "local"
    assert settings.log_level == "INFO"
    assert settings.docs_enabled is True
    assert settings.api_key_enabled is True
    assert settings.api_key_header == "X-API-Key"
    assert settings.link_ttl_hours == 168
    assert settings.privacy_policy_version == "v1"
    assert settings.twilio_signature_validation_enabled is True
    assert settings.phone_country_prefix == "51"
    assert settings.kizuna_callback_enabled is False
    assert settings.kizuna_callback_url == "http://kizuna-mock:8081/kizuna/consent-result"
    assert settings.kizuna_callback_max_retries == 3
    assert settings.kizuna_callback_backoff_seconds == [5, 30, 120]
    assert settings.kizuna_callback_timeout_seconds == 10


def test_comma_separated_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.com, https://b.com,")
    monkeypatch.setenv("KIZUNA_CALLBACK_BACKOFF_SECONDS", "1, 2.5,10")
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert settings.cors_allowed_origins == ["https://a.com", "https://b.com"]
    assert settings.kizuna_callback_backoff_seconds == [1, 2.5, 10]


def test_secrets_are_not_shown_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "super-secreta")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-twilio")
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert "super-secreta" not in repr(settings)
    assert "token-twilio" not in repr(settings)
    assert settings.api_key.get_secret_value() == "super-secreta"


def test_landing_base_url_trailing_slash_removed() -> None:
    settings = Settings(_env_file=None, database_url=DB_URL, landing_base_url="https://x.com/")
    assert settings.landing_base_url == "https://x.com"


# --- BackOffice (BACKOFFICE_DEALERS.spec.md, sección 9.1) ---


def test_backoffice_defaults_from_section_9_1() -> None:
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert settings.backoffice_base_url == ""
    assert settings.backoffice_session_cookie_secure is True
    assert settings.backoffice_session_idle_minutes == 30
    assert settings.backoffice_session_max_hours == 12
    assert settings.backoffice_login_max_failed_attempts == 5
    assert settings.backoffice_login_lock_minutes == 15
    assert settings.backoffice_password_min_length == 8
    assert settings.backoffice_password_reset_ttl_minutes == 30
    assert settings.backoffice_password_reset_max_per_hour == 3
    assert settings.email_send_enabled is False
    assert settings.email_from_address == ""
    assert settings.email_from_name == "BackOffice Consentimientos Yamaha"
    assert settings.smtp_host == ""
    assert settings.smtp_port == 587
    assert settings.smtp_security == "starttls"
    assert settings.smtp_username == ""
    assert settings.smtp_password.get_secret_value() == ""
    assert settings.smtp_timeout_seconds == 10


@pytest.mark.parametrize("value", ["https://bo.x.com/", "https://bo.x.com//"])
def test_backoffice_base_url_trailing_slash_removed(value: str) -> None:
    settings = Settings(_env_file=None, database_url=DB_URL, backoffice_base_url=value)
    assert settings.backoffice_base_url == "https://bo.x.com"


def test_backoffice_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKOFFICE_BASE_URL", "https://consentimientos-bo-test.bcscode.com/")
    monkeypatch.setenv("BACKOFFICE_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("BACKOFFICE_PASSWORD_MIN_LENGTH", "64")
    monkeypatch.setenv("SMTP_SECURITY", " SSL ")
    monkeypatch.setenv("SMTP_PORT", "465")
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert settings.backoffice_base_url == "https://consentimientos-bo-test.bcscode.com"
    assert settings.backoffice_session_cookie_secure is False
    assert settings.backoffice_password_min_length == 64
    assert settings.smtp_security == "ssl"
    assert settings.smtp_port == 465


def test_smtp_password_is_not_shown_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "clave-de-aplicacion")
    settings = Settings(_env_file=None, database_url=DB_URL)
    assert "clave-de-aplicacion" not in repr(settings)
    assert settings.smtp_password.get_secret_value() == "clave-de-aplicacion"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_env", "dev"),
        ("link_ttl_hours", 0),
        ("kizuna_callback_backoff_seconds", "5,-1"),
        # BackOffice: enteros > 0, largo mínimo de contraseña 8–64, puerto 1–65535, seguridad SMTP del catálogo.
        ("backoffice_session_idle_minutes", 0),
        ("backoffice_session_max_hours", 0),
        ("backoffice_login_max_failed_attempts", 0),
        ("backoffice_login_lock_minutes", -1),
        ("backoffice_password_min_length", 7),
        ("backoffice_password_min_length", 65),
        ("backoffice_password_reset_ttl_minutes", 0),
        ("backoffice_password_reset_max_per_hour", 0),
        ("smtp_port", 0),
        ("smtp_port", 65536),
        ("smtp_security", "tls"),
        ("smtp_timeout_seconds", 0),
    ],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DB_URL, **{field: value})
