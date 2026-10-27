"""Seguridad de la API (M2, sección 2.1): API Key para los endpoints de KIZUNA y CORS para la landing.

Los endpoints públicos (/api/v1/public/*) no usan API Key: los protege el token del enlace.
"""

import logging
import secrets
from typing import Annotated, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

MISSING_API_KEY = "Falta la API Key"
INVALID_API_KEY = "API Key inválida"

# Solo documenta la API Key en OpenAPI (botón "Authorize" de Swagger). La validación real está en
# require_api_key, que lee el header configurado en cada petición.
api_key_scheme = APIKeyHeader(
    name=get_settings().api_key_header,
    scheme_name="ApiKey",
    description="API Key de KIZUNA",
    auto_error=False,
)


async def require_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    _documented: Annotated[str | None, Security(api_key_scheme)] = None,
) -> None:
    """Dependencia de los endpoints de KIZUNA. Sin clave o con clave inválida → 401.

    Con API_KEY_ENABLED=false no valida nada (pruebas locales o con Postman).
    """
    if not settings.api_key_enabled:
        return

    provided = request.headers.get(settings.api_key_header)
    if not provided:
        _reject(request, MISSING_API_KEY)

    expected = settings.api_key.get_secret_value()
    # La app no arranca sin API_KEY (check_security_settings); igual se rechaza por si se usa la dependencia en otra
    # app. Comparación exacta y en bytes: compare_digest solo acepta str ASCII y el header podría traer otros
    # caracteres.
    if not expected or not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        _reject(request, INVALID_API_KEY)


def _reject(request: Request, detail: str) -> NoReturn:
    # Nunca se loguea la clave recibida ni la IP del cliente.
    route = request.scope.get("route")
    logger.warning("API Key rechazada: %s", detail, extra={"path": getattr(route, "path", request.url.path)})
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


ApiKeyDep = Depends(require_api_key)


class InsecureConfigurationError(RuntimeError):
    """Configuración de seguridad inválida: la aplicación no debe arrancar."""


def check_security_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app.

    - API_KEY_ENABLED=false → log WARNING (validación desactivada).
    - API_KEY_ENABLED=true y API_KEY vacía (o solo espacios) → InsecureConfigurationError: la app no arranca y el
      despliegue en Cloud Run falla, en lugar de quedar una API que rechaza todo.
    """
    if not settings.api_key_enabled:
        logger.warning("API_KEY_ENABLED=false: la validación de API Key está DESACTIVADA")
        return
    if not settings.api_key.get_secret_value().strip():
        message = "API_KEY_ENABLED=true pero API_KEY está vacía: configure API_KEY o desactive la validación"
        logger.critical(message)
        raise InsecureConfigurationError(message)


def add_cors(app: FastAPI, settings: Settings) -> None:
    """CORS solo para la landing (CORS_ALLOWED_ORIGINS).

    La landing hace GET y POST con JSON y sin cookies. KIZUNA llama servidor a servidor y no necesita CORS, por eso
    el header de la API Key no está permitido desde el navegador.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
        expose_headers=["X-Request-ID"],
    )
