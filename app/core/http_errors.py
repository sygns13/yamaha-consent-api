"""Errores HTTP en español (404, 405, 400 por body ilegible, 500, ...).

FastAPI y Starlette generan algunos errores con textos en inglés ("Not Found", "Method Not Allowed",
"There was an error parsing the body") y el 500 en texto plano. Aquí se traducen y todos responden
{"detail": "..."} en JSON. Los `detail` que ya escribimos en español (p. ej. los 401 de la API Key) pasan sin cambios.
Los 422 de validación están en validation_errors.py.
"""

import logging
from http import HTTPStatus

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

BODY_PARSE_ERROR = "No se pudo leer el cuerpo de la petición: debe ser JSON en UTF-8"
INTERNAL_ERROR = "Error interno del servidor"

# Textos en inglés que generan FastAPI/Starlette (además del nombre estándar de cada código HTTP).
_FRAMEWORK_DETAILS = {
    "There was an error parsing the body": BODY_PARSE_ERROR,
}

_STATUS_MESSAGES = {
    400: "Solicitud inválida",
    401: "No autorizado",
    403: "Acceso denegado",
    404: "Recurso no encontrado",
    405: "Método no permitido",
    406: "Formato de respuesta no aceptable",
    409: "Conflicto con el estado actual del recurso",
    410: "El recurso ya no está disponible",
    413: "El cuerpo de la petición es demasiado grande",
    415: "Tipo de contenido no soportado",
    422: "Datos inválidos",
    429: "Demasiadas peticiones",
    500: INTERNAL_ERROR,
    502: "Error en un servicio externo",
    503: "Servicio no disponible",
    504: "Tiempo de espera agotado en un servicio externo",
}


def translate_http_detail(status_code: int, detail: object) -> object:
    """Traduce el `detail` si es un texto por defecto del framework; si no, lo devuelve tal cual."""
    if not isinstance(detail, str):
        return detail
    if detail in _FRAMEWORK_DETAILS:
        return _FRAMEWORK_DETAILS[detail]
    try:
        is_default_phrase = detail == HTTPStatus(status_code).phrase
    except ValueError:  # código no estándar
        is_default_phrase = False
    if is_default_phrase or not detail:
        return _STATUS_MESSAGES.get(status_code, f"Error HTTP {status_code}")
    return detail


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Se conservan los headers del error (p. ej. Allow en el 405).
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": translate_http_detail(exc.status_code, exc.detail)},
        headers=exc.headers,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """500 en JSON y en español, sin detalles internos.

    Starlette vuelve a lanzar la excepción después de responder, así que Uvicorn la registra con su traceback.
    """
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": INTERNAL_ERROR})
