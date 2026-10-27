"""Respuesta 422 con mensajes en español (M2, sección 2.2).

Pydantic no traduce sus mensajes: aquí se arma cada uno a partir del `type` y el `ctx` del error. Los ValueError
propios (app/utils/validation.py) ya vienen en español y se usan tal cual, sin el prefijo "Value error, ".

La respuesta no incluye el valor recibido (`input`): podría ser un dato personal (documento, nombre, teléfono).
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

DEFAULT_MESSAGE = "Valor inválido"

Context = Mapping[str, Any]


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _number(value: Any) -> str:
    # En campos float Pydantic guarda los límites como 10.0: se muestran como 10.
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _options(ctx: Context) -> str:
    # Pydantic arma `expected` como "'A', 'B' or 'C'".
    expected = str(ctx["expected"])
    head, sep, tail = expected.rpartition(" or ")
    return f"{head} o {tail}" if sep else expected


def _string_too_short(ctx: Context) -> str:
    if ctx["min_length"] == 1:
        return "No puede estar vacío"
    return f"Debe tener al menos {_plural(ctx['min_length'], 'carácter', 'caracteres')}"


def _error_message(ctx: Context) -> str:
    # ValueError / AssertionError de nuestros validadores: ya viene en español.
    return str(ctx["error"])


_MESSAGES: dict[str, str | Callable[[Context], str]] = {
    "missing": "Campo obligatorio",
    "extra_forbidden": "Campo no permitido",
    "value_error": _error_message,
    "assertion_error": _error_message,
    # Textos
    "string_type": "Debe ser un texto",
    "string_too_short": _string_too_short,
    "string_too_long": lambda ctx: (
        f"Debe tener como máximo {_plural(ctx['max_length'], 'carácter', 'caracteres')}"
    ),
    "string_pattern_mismatch": "No tiene el formato esperado",
    "string_unicode": "Debe ser un texto Unicode válido",
    # Opciones
    "enum": lambda ctx: f"Debe ser uno de estos valores: {_options(ctx)}",
    "literal_error": lambda ctx: f"Debe ser uno de estos valores: {_options(ctx)}",
    # Objetos y listas
    "dict_type": "Debe ser un objeto JSON",
    "model_type": "Debe ser un objeto JSON",
    "model_attributes_type": "Debe ser un objeto JSON",
    "list_type": "Debe ser una lista",
    "too_short": lambda ctx: f"Debe tener al menos {_plural(ctx['min_length'], 'elemento', 'elementos')}",
    "too_long": lambda ctx: f"Debe tener como máximo {_plural(ctx['max_length'], 'elemento', 'elementos')}",
    # Booleanos y números
    "bool_type": "Debe ser verdadero o falso (true/false)",
    "bool_parsing": "Debe ser verdadero o falso (true/false)",
    "int_type": "Debe ser un número entero",
    "int_parsing": "Debe ser un número entero",
    "int_from_float": "Debe ser un número entero, sin decimales",
    "float_type": "Debe ser un número",
    "float_parsing": "Debe ser un número",
    "finite_number": "Debe ser un número finito",
    "greater_than": lambda ctx: f"Debe ser mayor que {_number(ctx['gt'])}",
    "greater_than_equal": lambda ctx: f"Debe ser mayor o igual que {_number(ctx['ge'])}",
    "less_than": lambda ctx: f"Debe ser menor que {_number(ctx['lt'])}",
    "less_than_equal": lambda ctx: f"Debe ser menor o igual que {_number(ctx['le'])}",
    # Fechas, UUID, URL
    "datetime_type": "Debe ser una fecha y hora válida (ISO 8601)",
    "datetime_parsing": "Debe ser una fecha y hora válida (ISO 8601)",
    "datetime_from_date_parsing": "Debe ser una fecha y hora válida (ISO 8601)",
    "date_type": "Debe ser una fecha válida (AAAA-MM-DD)",
    "date_parsing": "Debe ser una fecha válida (AAAA-MM-DD)",
    "date_from_datetime_parsing": "Debe ser una fecha válida (AAAA-MM-DD)",
    "uuid_type": "Debe ser un UUID válido",
    "uuid_parsing": "Debe ser un UUID válido",
    "url_type": "Debe ser una URL válida",
    "url_parsing": "Debe ser una URL válida",
    "none_required": "Debe ser nulo",
    # Cuerpo de la petición
    "json_invalid": "El cuerpo de la petición no es un JSON válido",
    "json_type": "Debe ser un JSON válido",
}

# Errores sobre el cuerpo completo (loc == ("body",)).
_BODY_MESSAGES = {
    "missing": "El cuerpo de la petición es obligatorio",
    "model_attributes_type": "El cuerpo de la petición debe ser un objeto JSON",
    "model_type": "El cuerpo de la petición debe ser un objeto JSON",
}


def translate_message(error: Mapping[str, Any]) -> str:
    """Mensaje en español para un error de Pydantic. Si el tipo no está traducido: "Valor inválido"."""
    error_type = error.get("type", "")
    if tuple(error.get("loc", ())) == ("body",) and error_type in _BODY_MESSAGES:
        return _BODY_MESSAGES[error_type]
    template = _MESSAGES.get(error_type)
    if template is None:
        return DEFAULT_MESSAGE
    if isinstance(template, str):
        return template
    try:
        return template(error.get("ctx") or {})
    except (KeyError, TypeError):
        return DEFAULT_MESSAGE


def translate_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """[{type, loc, msg}] con `msg` en español. Se descartan `input`, `ctx` y `url`."""
    return [
        {"type": error.get("type"), "loc": list(error.get("loc", ())), "msg": translate_message(error)}
        for error in errors
    ]


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail = translate_errors(exc.errors())
    # Solo campo y tipo de error: nunca el valor recibido.
    logger.warning(
        "Petición rechazada por validación (422)",
        extra={"errors": [{"loc": item["loc"], "type": item["type"]} for item in detail]},
    )
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": detail}))
