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


class BackofficeHeadersMiddleware:
    """Cabeceras de las respuestas bajo /api/v1/backoffice (BACKOFFICE_DEALERS.spec.md, sección 5.7).

    `Cache-Control: no-store` (ni el CDN de Firebase ni el navegador guardan datos del BackOffice) y
    `X-Content-Type-Options: nosniff`. Aplica a toda respuesta bajo el prefijo, también a los errores.
    """

    _HEADERS = ((b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"))

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path != self.prefix and not path.startswith(self.prefix + "/"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in self._HEADERS}
                headers = [
                    (name, value) for name, value in message.get("headers", []) if name.lower() not in names
                ]
                headers.extend(self._HEADERS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
