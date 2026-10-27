import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"
# Solo se reutiliza un X-Request-ID entrante si es corto y seguro; si no, se genera uno nuevo.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = logging.getLogger("app.request")


class RequestIdMiddleware:
    """Asigna un request_id a cada petición, lo devuelve en X-Request-ID y loguea una línea por petición.

    El log usa la plantilla de la ruta (/api/v1/public/consent-links/{token}) en lugar de la ruta real, para
    no escribir el token del enlace, y nunca incluye la query string (puede traer el número de documento)
    ni la IP del cliente.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status_code = 500
        start = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode("latin-1"), request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "%s %s %s",
                scope["method"],
                _route_path(scope),
                status_code,
                extra={
                    "http_method": scope["method"],
                    "path": _route_path(scope),
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
            request_id_var.reset(token)


def _incoming_request_id(scope: Scope) -> str | None:
    header = REQUEST_ID_HEADER.lower().encode("latin-1")
    for name, value in scope.get("headers", []):
        if name == header:
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


def _route_path(scope: Scope) -> str:
    # El router de Starlette deja la ruta resuelta en scope["route"] (FastAPI APIRoute).
    route = scope.get("route")
    return getattr(route, "path", None) or scope["path"]
