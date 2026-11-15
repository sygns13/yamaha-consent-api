"""Seguridad del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 5).

Por ahora solo el chequeo de configuración al arrancar (sección 5.12). Las dependencias de sesión, módulo, cambio
obligatorio de contraseña y cabecera CSRF se agregan en la fase B2.
"""

import logging

from app.core.config import Settings
from app.core.security import InsecureConfigurationError

logger = logging.getLogger(__name__)


def check_backoffice_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app.

    Con APP_ENV=production y BACKOFFICE_SESSION_COOKIE_SECURE=false → InsecureConfigurationError (log CRITICAL): la
    cookie de sesión viajaría también por http, así que la app no arranca y el despliegue en Cloud Run falla.
    """
    if settings.app_env == "production" and not settings.backoffice_session_cookie_secure:
        message = (
            "APP_ENV=production con BACKOFFICE_SESSION_COOKIE_SECURE=false: la cookie de sesión del BackOffice "
            "debe ser Secure en Producción"
        )
        logger.critical(message)
        raise InsecureConfigurationError(message)
