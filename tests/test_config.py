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


@pytest.mark.parametrize(
    ("field", "value"),
    [("app_env", "dev"), ("link_ttl_hours", 0), ("kizuna_callback_backoff_seconds", "5,-1")],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DB_URL, **{field: value})
