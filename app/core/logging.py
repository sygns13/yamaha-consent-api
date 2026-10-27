"""Logging estructurado en JSON a stdout (Cloud Logging) con el request_id de la petición en curso.

Los datos personales (documento, nombre, teléfono, IP) se enmascaran antes de loguearlos: ver app/utils/masking.py.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Atributos estándar de LogRecord: todo lo demás se considera un campo `extra` y se agrega al JSON.
# color_message es un extra de Uvicorn con códigos ANSI.
_RESERVED_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    """Una línea JSON por registro. `severity` y `message` son los campos que reconoce Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    """Deja un único handler JSON en el logger raíz y hace que los loggers de Uvicorn lo usen."""
    # En Windows, stdout redirigido usa cp1252; los logs llevan tildes y Ñ.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # El access log de Uvicorn escribe la IP del cliente sin enmascarar; lo reemplaza RequestIdMiddleware.
    logging.getLogger("uvicorn.access").disabled = True
    # El SDK de Twilio loguea en INFO los parámetros y cabeceras de cada petición.
    logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
