"""Verificación del ID token de Google de KIZUNA dentro de la API (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, 4.3).

Defensa en profundidad del servicio privado `consent-api-kizuna`: Cloud Run IAM ya validó el token antes de que la
petición llegue aquí, pero la API vuelve a verificarlo para que un error de despliegue (servicio público por error,
o un Owner/Editor del proyecto con su propio token) no deje entrar a nadie más que a la cuenta de servicio de KIZUNA.

Se verifica:
- firma RS256 con las claves públicas de Google (`GOOGLE_CERTS_URL`, en caché según su `Cache-Control`);
- `iss` de Google, `exp`/`iat` (con `LEEWAY_SECONDS` de tolerancia), `aud` dentro de `KIZUNA_ID_TOKEN_AUDIENCES`;
- `email` = `KIZUNA_ID_TOKEN_EMAIL` y `email_verified` = true.

Nunca se loguea el token ni sus claims: solo el motivo del rechazo.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from functools import lru_cache
from typing import Any

import httpx
import jwt

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
LEEWAY_SECONDS = 30
DEFAULT_CERTS_TTL_SECONDS = 3600.0
# Un `kid` desconocido fuerza una recarga de las claves, como mucho una vez por este intervalo.
MIN_REFRESH_INTERVAL_SECONDS = 60.0
CERTS_TIMEOUT_SECONDS = 10.0

# Devuelve (JWKS de Google, segundos de vigencia).
CertsFetcher = Callable[[], Awaitable[tuple[dict[str, Any], float]]]


class IdTokenError(Exception):
    """Token rechazado. `status_code`: 401 (token inválido), 403 (otra identidad) o 503 (claves no disponibles)."""

    def __init__(self, reason: str, status_code: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _max_age(cache_control: str | None) -> float:
    match = re.search(r"max-age=(\d+)", cache_control or "")
    return float(match.group(1)) if match else DEFAULT_CERTS_TTL_SECONDS


async def fetch_google_certs(client: httpx.AsyncClient | None = None) -> tuple[dict[str, Any], float]:
    """Claves públicas de Google (JWKS) y su vigencia según `Cache-Control: max-age` (1 h si no viene)."""
    if client is None:
        async with httpx.AsyncClient(timeout=CERTS_TIMEOUT_SECONDS) as own_client:
            return await fetch_google_certs(own_client)
    response = await client.get(GOOGLE_CERTS_URL)
    response.raise_for_status()
    return response.json(), _max_age(response.headers.get("cache-control"))


class GoogleIdTokenVerifier:
    """Verifica ID tokens de Google. Uno por proceso (`get_google_id_token_verifier`): las claves quedan en caché."""

    def __init__(
        self, fetch: CertsFetcher = fetch_google_certs, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._last_fetch: float | None = None
        self._lock = asyncio.Lock()

    async def verify(self, token: str, *, audiences: Sequence[str], email: str) -> None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise IdTokenError("token mal formado") from exc
        if header.get("alg") != "RS256":
            raise IdTokenError("algoritmo no admitido")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise IdTokenError("token sin kid")

        key = await self._key_for(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=list(audiences),
                issuer=GOOGLE_ISSUERS,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise IdTokenError("token vencido") from exc
        except jwt.InvalidAudienceError as exc:
            raise IdTokenError("audiencia distinta") from exc
        except jwt.InvalidIssuerError as exc:
            raise IdTokenError("emisor distinto de Google") from exc
        except jwt.PyJWTError as exc:
            raise IdTokenError("firma o claims inválidos") from exc

        token_email = claims.get("email")
        if not isinstance(token_email, str) or token_email.lower() != email.lower():
            raise IdTokenError("identidad distinta de la cuenta de servicio de KIZUNA", status_code=403)
        if claims.get("email_verified") is not True:
            raise IdTokenError("email del token sin verificar", status_code=403)

    async def _key_for(self, kid: str) -> Any:
        key = self._cached_key(kid)
        if key is not None:
            return key
        async with self._lock:
            key = self._cached_key(kid)
            if key is None and self._may_refresh():
                await self._refresh()
                key = self._cached_key(kid)
        if key is None:
            raise IdTokenError("clave de firma desconocida")
        return key

    def _cached_key(self, kid: str) -> Any:
        if self._clock() >= self._expires_at:
            return None
        return self._keys.get(kid)

    def _may_refresh(self) -> bool:
        now = self._clock()
        expired = now >= self._expires_at
        return expired or self._last_fetch is None or now - self._last_fetch >= MIN_REFRESH_INTERVAL_SECONDS

    async def _refresh(self) -> None:
        self._last_fetch = self._clock()
        try:
            jwks, ttl = await self._fetch()
            keys = {
                item["kid"]: jwt.PyJWK(item, algorithm="RS256").key
                for item in jwks.get("keys", [])
                if isinstance(item, dict) and item.get("kid")
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, jwt.PyJWTError) as exc:
            raise IdTokenError("no se pudieron obtener las claves de Google", status_code=503) from exc
        self._keys = keys
        self._expires_at = self._clock() + max(ttl, 0.0)


@lru_cache
def get_google_id_token_verifier() -> GoogleIdTokenVerifier:
    return GoogleIdTokenVerifier()
