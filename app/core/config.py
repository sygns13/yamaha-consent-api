from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    # --- Endpoints expuestos y autenticación de KIZUNA (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4) ---
    # all: todos (local y VPS). public: sin los de KIZUNA (Cloud Run consent-api). kizuna: solo los de KIZUNA y
    # /health (Cloud Run consent-api-kizuna, privado).
    api_surface: Literal["all", "public", "kizuna"] = "all"
    # api_key: X-API-Key según API_KEY_ENABLED. cloud_run_iam: la API no valida credenciales porque Cloud Run ya exigió
    # el ID token de la cuenta de servicio de KIZUNA; solo se admite con API_SURFACE=kizuna.
    kizuna_inbound_auth: Literal["api_key", "cloud_run_iam"] = "api_key"

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
    # TODO(PENDIENTE-27): URL y respuesta esperada del callback real (el payload ya está definido; la autenticación es
    # OAuth 2.0, INTEGRACION_KIZUNA_OAUTH2.spec.md).
    kizuna_callback_enabled: bool = False
    kizuna_callback_url: str = "http://kizuna-mock:8081/kizuna/consent-result"
    # header: KIZUNA_CALLBACK_AUTH_HEADER/_VALUE (header vacío = sin autenticación; comportamiento de M5).
    # oauth2: client credentials contra el servidor de autorización de KIZUNA (Entra ID), sección 4 del spec OAuth2.
    kizuna_callback_auth_mode: Literal["header", "oauth2"] = "header"
    kizuna_callback_auth_header: str = ""
    kizuna_callback_auth_value: SecretStr = SecretStr("")
    # TODO(PENDIENTE-26): token URL, client_id, client_secret y scope que entregue KIZUNA.
    kizuna_oauth_token_url: str = ""
    kizuna_oauth_client_id: str = ""
    kizuna_oauth_client_secret: SecretStr = SecretStr("")
    # Un solo valor. En Entra ID con client credentials: <recurso>/.default.
    kizuna_oauth_scope: str = ""
    kizuna_oauth_client_auth_method: Literal["client_secret_post", "client_secret_basic"] = (
        "client_secret_post"
    )
    # El token se renueva este tiempo antes de su vencimiento (no cambia su vida, 3600 s).
    kizuna_oauth_refresh_margin_seconds: int = Field(default=300, ge=0, le=1800)
    kizuna_callback_max_retries: int = Field(default=3, ge=0)
    kizuna_callback_backoff_seconds: Annotated[list[float], NoDecode] = [5, 30, 120]
    kizuna_callback_timeout_seconds: float = Field(default=10, gt=0)
    # Al arrancar se reenvían las notificaciones PENDIENTE sin reclamar o con el reclamo vencido (más de N minutos
    # sin intento).
    kizuna_callback_reprocess_after_minutes: float = Field(default=10, gt=0)
    # Zona horaria de las fechas que se intercambian con KIZUNA: consentDateTime, las fechas de las respuestas y
    # requestDateTime cuando llega sin offset.
    kizuna_timezone: str = "America/Lima"

    # --- BackOffice (BACKOFFICE_DEALERS.spec.md, sección 9.1) ---
    # TODO(PENDIENTE-18): dominio del BackOffice en Producción. Mientras tanto, la URL técnica de Firebase Hosting.
    # Base del enlace de restablecimiento de contraseña: {BACKOFFICE_BASE_URL}/restablecer-contrasena?t={token}.
    backoffice_base_url: str = ""
    # false solo en local por http. Con APP_ENV=production y false, la app no arranca (check_backoffice_settings).
    backoffice_session_cookie_secure: bool = True
    backoffice_session_idle_minutes: int = Field(default=30, gt=0)
    backoffice_session_max_hours: int = Field(default=12, gt=0)
    backoffice_login_max_failed_attempts: int = Field(default=5, gt=0)
    backoffice_login_lock_minutes: int = Field(default=15, gt=0)
    # Rango 8–20: el máximo de la política es 20 (spec v1.2, sección 5.1).
    backoffice_password_min_length: int = Field(default=8, ge=8, le=20)
    backoffice_password_reset_ttl_minutes: int = Field(default=30, gt=0)
    backoffice_password_reset_max_per_hour: int = Field(default=3, gt=0)

    # --- Correo del BackOffice (SMTP, sección 7) ---
    # false: no se envían correos reales (solo se registra en el log). true: envío por SMTP.
    email_send_enabled: bool = False
    email_from_address: str = ""
    email_from_name: str = "BackOffice Consentimientos Yamaha"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_timeout_seconds: float = Field(default=10, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "smtp_security",
        "kizuna_callback_auth_mode",
        "kizuna_oauth_client_auth_method",
        "api_surface",
        "kizuna_inbound_auth",
        mode="before",
    )
    @classmethod
    def _lower_choice(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("kizuna_oauth_token_url", "kizuna_oauth_client_id", "kizuna_oauth_scope")
    @classmethod
    def _strip_oauth_value(cls, value: str) -> str:
        return value.strip()

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

    @field_validator("kizuna_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"zona horaria desconocida: {value}") from exc
        return value

    @field_validator("landing_base_url", "backoffice_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # Los enlaces se arman como {LANDING_BASE_URL}/?t={token} y {BACKOFFICE_BASE_URL}/restablecer-contrasena?t=...
        return value.rstrip("/")

    @property
    def kizuna_zone(self) -> ZoneInfo:
        return ZoneInfo(self.kizuna_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
