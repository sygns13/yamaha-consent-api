from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    """Convierte "a, b,c" en ["a", "b", "c"]. Las listas se dejan como vienen."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    """Configuración de la aplicación (sección 9). Todo se lee de variables de entorno o del archivo .env.

    Los secretos son SecretStr: no se muestran en repr ni en logs; se leen con .get_secret_value().
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    app_env: Literal["local", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # TODO(PENDIENTE-12): ¿se deja /docs expuesto en Producción?
    docs_enabled: bool = True

    # --- Base de datos ---
    database_url: str

    # --- Seguridad (endpoints de KIZUNA) ---
    api_key_enabled: bool = True
    api_key_header: str = "X-API-Key"
    api_key: SecretStr = SecretStr("")
    # TODO(PENDIENTE-15): dominio final de la landing.
    cors_allowed_origins: Annotated[list[str], NoDecode] = []

    # --- Enlaces y landing ---
    link_ttl_hours: int = Field(default=168, gt=0)
    # TODO(PENDIENTE-15): dominio final de la landing.
    landing_base_url: str = ""
    # TODO(PENDIENTE-14): URL y versión vigentes de la política de privacidad.
    privacy_policy_url: str = ""
    privacy_policy_version: str = "v1"

    # --- Twilio WhatsApp ---
    # false: no se envían WhatsApp reales (FakeWhatsAppSender solo registra en el log). true: Twilio.
    twilio_send_enabled: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_whatsapp_from: str = ""
    twilio_content_sid: str = ""
    twilio_status_callback_url: str = ""
    twilio_signature_validation_enabled: bool = True
    # El envío ocurre dentro de la transacción de POST /consents (con el bloqueo del cliente tomado).
    twilio_timeout_seconds: float = Field(default=10, gt=0)
    phone_country_prefix: str = "51"

    # --- Notificación a KIZUNA ---
    # TODO(PENDIENTE-13): URL, autenticación y contrato reales del callback.
    kizuna_callback_enabled: bool = False
    kizuna_callback_url: str = "http://kizuna-mock:8081/kizuna/consent-result"
    kizuna_callback_auth_header: str = ""
    kizuna_callback_auth_value: SecretStr = SecretStr("")
    kizuna_callback_max_retries: int = Field(default=3, ge=0)
    kizuna_callback_backoff_seconds: Annotated[list[float], NoDecode] = [5, 30, 120]
    kizuna_callback_timeout_seconds: float = Field(default=10, gt=0)
    # Al arrancar se reenvían las notificaciones PENDIENTE sin reclamar o con el reclamo vencido (más de N minutos
    # sin intento).
    kizuna_callback_reprocess_after_minutes: float = Field(default=10, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_allowed_origins", "kizuna_callback_backoff_seconds", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("kizuna_callback_backoff_seconds")
    @classmethod
    def _non_negative_backoff(cls, value: list[float]) -> list[float]:
        if any(seconds < 0 for seconds in value):
            raise ValueError("los tiempos de espera no pueden ser negativos")
        return value

    @field_validator("landing_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # El enlace se arma como {LANDING_BASE_URL}/?t={token}.
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
