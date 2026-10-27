from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de la aplicación. Todo se lee de variables de entorno o del archivo .env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "local"
    log_level: str = "INFO"
    docs_enabled: bool = True

    database_url: str

    api_key_enabled: bool = True
    api_key_header: str = "X-API-Key"
    api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
