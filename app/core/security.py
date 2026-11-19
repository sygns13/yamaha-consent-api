"""Seguridad de la API (M2, sección 2.1): autenticación de los endpoints de KIZUNA y CORS para la landing.

Los endpoints públicos (/api/v1/public/*) no usan API Key: los protege el token del enlace.

KIZUNA se autentica de dos formas (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4):
- API Key (`X-API-Key`), en local y en el VPS (KIZUNA_INBOUND_AUTH=api_key, por defecto).
- En Producción, el servicio privado de Cloud Run (KIZUNA_INBOUND_AUTH=cloud_run_iam y API_SURFACE=kizuna): Cloud Run
  IAM valida el ID token de Google de la cuenta de servicio de KIZUNA y la API lo vuelve a verificar (firma, audiencia
  y email; `app/core/google_id_token.py`). La API Key no se usa.
"""

import logging
import secrets
from typing import Annotated, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings
from app.core.google_id_token import GoogleIdTokenVerifier, IdTokenError, get_google_id_token_verifier

logger = logging.getLogger(__name__)

MISSING_API_KEY = "Falta la API Key"
INVALID_API_KEY = "API Key inválida"
MISSING_ID_TOKEN = "Falta el token de acceso"
INVALID_ID_TOKEN = "Token de acceso inválido o vencido"
FORBIDDEN_ID_TOKEN = "El token no corresponde a la cuenta autorizada"
ID_TOKEN_UNAVAILABLE = "No se pudo validar el token de acceso; reintente"
ID_TOKEN_DETAILS = {401: INVALID_ID_TOKEN, 403: FORBIDDEN_ID_TOKEN, 503: ID_TOKEN_UNAVAILABLE}

# Solo documenta la API Key en OpenAPI (botón "Authorize" de Swagger). La validación real está en
# require_kizuna_auth, que lee el header configurado en cada petición.
api_key_scheme = APIKeyHeader(
    name=get_settings().api_key_header,
    scheme_name="ApiKey",
    description="API Key de KIZUNA",
    auto_error=False,
)


def uses_google_id_token(settings: Settings) -> bool:
    """True solo en el servicio privado de KIZUNA: cloud_run_iam y API_SURFACE=kizuna (las dos condiciones)."""
    return settings.kizuna_inbound_auth == "cloud_run_iam" and settings.api_surface == "kizuna"


async def require_kizuna_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    verifier: Annotated[GoogleIdTokenVerifier, Depends(get_google_id_token_verifier)],
    _documented: Annotated[str | None, Security(api_key_scheme)] = None,
) -> None:
    """Dependencia de los endpoints de KIZUNA.

    - Servicio privado (cloud_run_iam): `Authorization: Bearer <ID token de Google>` de la cuenta de servicio de
      KIZUNA. Sin token o inválido → 401; otra identidad → 403; claves de Google no disponibles → 503.
    - Si no: API Key. Sin clave o con clave inválida → 401. Con API_KEY_ENABLED=false no valida nada (pruebas locales
      o con Postman).
    """
    if uses_google_id_token(settings):
        await _require_google_id_token(request, settings, verifier)
        return
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


async def _require_google_id_token(
    request: Request, settings: Settings, verifier: GoogleIdTokenVerifier
) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        _reject_id_token(request, status.HTTP_401_UNAUTHORIZED, MISSING_ID_TOKEN, "sin Bearer")
    try:
        await verifier.verify(
            token, audiences=settings.kizuna_id_token_audiences, email=settings.kizuna_id_token_email
        )
    except IdTokenError as exc:
        _reject_id_token(request, exc.status_code, ID_TOKEN_DETAILS[exc.status_code], exc.reason)


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def _reject(request: Request, detail: str) -> NoReturn:
    # Nunca se loguea la clave recibida ni la IP del cliente.
    logger.warning("API Key rechazada: %s", detail, extra={"path": _route_path(request)})
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _reject_id_token(request: Request, status_code: int, detail: str, reason: str) -> NoReturn:
    # Solo el motivo: nunca el token, sus claims ni la IP del cliente.
    logger.warning("ID token de KIZUNA rechazado: %s", reason, extra={"path": _route_path(request)})
    headers = {"WWW-Authenticate": "Bearer"} if status_code == status.HTTP_401_UNAUTHORIZED else None
    raise HTTPException(status_code=status_code, detail=detail, headers=headers)


KizunaAuthDep = Depends(require_kizuna_auth)


class InsecureConfigurationError(RuntimeError):
    """Configuración de seguridad inválida: la aplicación no debe arrancar."""


def _fail(message: str) -> NoReturn:
    logger.critical(message)
    raise InsecureConfigurationError(message)


def _id_token_problems(settings: Settings) -> list[str]:
    problems = []
    audiences = settings.kizuna_id_token_audiences
    email = settings.kizuna_id_token_email
    if not audiences:
        problems.append("falta KIZUNA_ID_TOKEN_AUDIENCES")
    if not email:
        problems.append("falta KIZUNA_ID_TOKEN_EMAIL")
    elif "@" not in email:
        problems.append("KIZUNA_ID_TOKEN_EMAIL no es un correo")
    if any("<" in value or ">" in value for value in [*audiences, email]):
        problems.append("KIZUNA_ID_TOKEN_* conserva un valor de ejemplo <...>")
    return problems


def check_security_settings(settings: Settings) -> None:
    """Se ejecuta al arrancar la app (spec Cloud Run IAM, sección 4.3).

    - KIZUNA_INBOUND_AUTH=cloud_run_iam con API_SURFACE distinto de kizuna → InsecureConfigurationError: un servicio
      público expondría los endpoints de KIZUNA sin la protección de Cloud Run IAM.
    - cloud_run_iam con API_SURFACE=kizuna: KIZUNA_ID_TOKEN_AUDIENCES y KIZUNA_ID_TOKEN_EMAIL obligatorios y sin
      marcadores <...> (si no, InsecureConfigurationError); log INFO de recordatorio.
    - API_SURFACE=public → no hay endpoints de KIZUNA: la API Key no se revisa.
    - API_KEY_ENABLED=false → log WARNING (validación desactivada).
    - API_KEY_ENABLED=true y API_KEY vacía (o solo espacios) → InsecureConfigurationError: la app no arranca y el
      despliegue en Cloud Run falla, en lugar de quedar una API que rechaza todo.
    """
    if settings.kizuna_inbound_auth == "cloud_run_iam":
        if settings.api_surface != "kizuna":
            _fail(
                "KIZUNA_INBOUND_AUTH=cloud_run_iam solo se admite con API_SURFACE=kizuna (servicio privado de Cloud Run)"
            )
        problems = _id_token_problems(settings)
        if problems:
            _fail("KIZUNA_INBOUND_AUTH=cloud_run_iam: " + "; ".join(problems))
        logger.info(
            "KIZUNA_INBOUND_AUTH=cloud_run_iam: la API verifica el ID token de KIZUNA (firma de Google, audiencia y "
            "email); el servicio de Cloud Run debe ser privado (roles/run.invoker solo para esa cuenta de servicio)"
        )
        return
    if settings.api_surface == "public":
        return
    if not settings.api_key_enabled:
        logger.warning("API_KEY_ENABLED=false: la validación de API Key está DESACTIVADA")
        return
    if not settings.api_key.get_secret_value().strip():
        _fail("API_KEY_ENABLED=true pero API_KEY está vacía: configure API_KEY o desactive la validación")


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
