"""Token de KIZUNA para el callback (INTEGRACION_KIZUNA_OAUTH2.spec.md, secciones 4.1, 4.2 y 4.4).

OAuth 2.0 client credentials contra el servidor de autorización de KIZUNA (Microsoft Entra ID). El token es opaco para
la API: no se decodifica ni se valida. Se reutiliza hasta KIZUNA_OAUTH_REFRESH_MARGIN_SECONDS antes de su vencimiento.

Hay un solo proveedor por proceso (get_kizuna_token_provider): lo comparten los envíos después del commit y el
reproceso al arrancar. No vive en KizunaClient, porque KizunaNotifier crea un cliente por lote.

Nunca se registran ni se guardan el client_secret, el access_token ni el cuerpo del pedido.
"""

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

_ERROR_MAX_LENGTH = 500
_REDACTED = "****"


class KizunaTokenError(Exception):
    """No se obtuvo el token. `retryable`: red, timeout, 5xx o 429 (lo demás deja la notificación FALLIDO)."""

    def __init__(self, error: str, *, retryable: bool, http_status: int | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.retryable = retryable
        self.http_status = http_status


@dataclass(frozen=True)
class _CachedToken:
    # Credenciales con las que se obtuvo: si la configuración cambia, no se reutiliza.
    key: tuple[str, str, str, str]
    value: str
    valid_until: float


class KizunaTokenProvider:
    """Obtiene y guarda en memoria el token de KIZUNA. Un solo pedido a la vez (asyncio.Lock)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()

    async def get_token(self, client: httpx.AsyncClient, settings: Settings) -> str:
        """Token vigente (de la caché o uno nuevo). Lanza KizunaTokenError si no se puede obtener."""
        key = _credentials_key(settings)
        if (token := self._cached_token(key)) is not None:
            return token
        async with self._lock:
            # Otro envío pudo obtenerlo mientras este esperaba el lock.
            if (token := self._cached_token(key)) is not None:
                return token
            token, expires_in = await _request_token(client, settings)
            if expires_in is None:
                # Sin vigencia conocida se usa una sola vez.
                self._cached = None
            else:
                valid_for = expires_in - settings.kizuna_oauth_refresh_margin_seconds
                self._cached = _CachedToken(key, token, self._clock() + valid_for)
            return token

    def invalidate(self, token: str) -> None:
        """Descarta el token que KIZUNA rechazó (401). Si ya se renovó, conserva el nuevo."""
        if self._cached is not None and self._cached.value == token:
            self._cached = None

    def _cached_token(self, key: tuple[str, str, str, str]) -> str | None:
        cached = self._cached
        if cached is None or cached.key != key or self._clock() >= cached.valid_until:
            return None
        return cached.value


@lru_cache
def get_kizuna_token_provider() -> KizunaTokenProvider:
    """Proveedor único del proceso. Los tests lo reinician con get_kizuna_token_provider.cache_clear()."""
    return KizunaTokenProvider()


@dataclass(frozen=True)
class KizunaTokenSource:
    """El proveedor del proceso con la configuración del envío en curso (lo que usa KizunaClient)."""

    provider: KizunaTokenProvider
    settings: Settings

    async def get(self, client: httpx.AsyncClient) -> str:
        return await self.provider.get_token(client, self.settings)

    def invalidate(self, token: str) -> None:
        self.provider.invalidate(token)


def _credentials_key(settings: Settings) -> tuple[str, str, str, str]:
    return (
        settings.kizuna_oauth_token_url,
        settings.kizuna_oauth_client_id,
        settings.kizuna_oauth_scope,
        settings.kizuna_oauth_client_auth_method,
    )


async def _request_token(client: httpx.AsyncClient, settings: Settings) -> tuple[str, float | None]:
    """Un pedido de token (client credentials). Devuelve (access_token, expires_in o None)."""
    client_id = settings.kizuna_oauth_client_id
    secret = settings.kizuna_oauth_client_secret.get_secret_value()
    data = {"grant_type": "client_credentials", "scope": settings.kizuna_oauth_scope}
    headers = {"Accept": "application/json"}
    if settings.kizuna_oauth_client_auth_method == "client_secret_basic":
        # RFC 6749 §2.3.1: id y secreto codificados como form-urlencoded antes del Base64.
        auth: httpx.Auth | None = httpx.BasicAuth(quote_plus(client_id), quote_plus(secret))
    else:
        auth = None
        data |= {"client_id": client_id, "client_secret": secret}

    timeout = settings.kizuna_callback_timeout_seconds
    try:
        async with asyncio.timeout(timeout):
            response = await client.post(
                settings.kizuna_oauth_token_url, data=data, headers=headers, auth=auth
            )
    except TimeoutError:
        raise _failed(f"Timeout: sin respuesta en {timeout:g} s", retryable=True) from None
    except httpx.TimeoutException as exc:
        raise _failed(f"Timeout: {type(exc).__name__}", retryable=True) from None
    except httpx.HTTPError as exc:
        raise _failed(_redact(f"{type(exc).__name__}: {exc}", secret), retryable=True) from None

    if not response.is_success:
        retryable = response.status_code >= 500 or response.status_code == 429
        raise _failed(
            _redact(_error_text(response), secret), retryable=retryable, http_status=response.status_code
        )

    body = _json_object(response)
    token = body.get("access_token")
    token_type = body.get("token_type")
    if not isinstance(token, str) or not token:
        raise _failed("Respuesta sin access_token", retryable=False, http_status=response.status_code)
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise _failed(
            "Respuesta con token_type distinto de Bearer", retryable=False, http_status=response.status_code
        )
    expires_in = _expires_in(body.get("expires_in"))
    logger.info("Token de KIZUNA obtenido", extra={"expires_in": expires_in})
    return token, expires_in


def _failed(error: str, *, retryable: bool, http_status: int | None = None) -> KizunaTokenError:
    level = logging.WARNING if retryable else logging.ERROR
    # Solo el status y el tipo de error: la descripción completa queda en last_error de la notificación.
    logger.log(
        level,
        "No se pudo obtener el token de KIZUNA",
        extra={"http_status": http_status, "retryable": retryable, "error": error.split(":", 1)[0]},
    )
    return KizunaTokenError(error[:_ERROR_MAX_LENGTH], retryable=retryable, http_status=http_status)


def _error_text(response: httpx.Response) -> str:
    """HTTP <status>, más `error` y `error_description` si la respuesta los trae (RFC 6749 §5.2)."""
    body = _json_object(response)
    parts = [f"HTTP {response.status_code}"]
    parts += [str(body[field]) for field in ("error", "error_description") if body.get(field)]
    return ": ".join(parts)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _expires_in(value: object) -> float | None:
    """Número o texto numérico (Entra ID envía un número). Sin valor, inválido o no positivo → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, _REDACTED) if secret else text
