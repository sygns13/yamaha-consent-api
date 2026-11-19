"""ID tokens "de Google" para los tests: un par RSA local, su JWKS y un descargador de claves falso."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.google_id_token import GoogleIdTokenVerifier

AUDIENCE = "consent-api-kizuna.proyecto-de-prueba"
EMAIL = "kizuna-caller@proyecto-de-prueba.iam.gserviceaccount.com"


class KeyPair:
    def __init__(self, kid: str = "clave-1") -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        return {**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def sign(self, claims: dict[str, Any], *, kid: str | None = None) -> str:
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": kid or self.kid})


def claims(**overrides: Any) -> dict[str, Any]:
    """Claims de un ID token de Google de la cuenta de servicio de KIZUNA (válido por 1 h)."""
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "azp": "123456789",
        "sub": "123456789",
        "email": EMAIL,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


class FakeCertsFetcher:
    """Devuelve el JWKS de `keys` (o lanza `error`) y cuenta las descargas."""

    def __init__(self, *keys: KeyPair, ttl: float = 3600.0) -> None:
        self.keys = list(keys)
        self.ttl = ttl
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self) -> tuple[dict[str, Any], float]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"keys": [key.jwk() for key in self.keys]}, self.ttl


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def verifier_with(
    *keys: KeyPair, clock: Callable[[], float] | None = None
) -> tuple[GoogleIdTokenVerifier, FakeCertsFetcher]:
    fetcher = FakeCertsFetcher(*keys)
    verifier = GoogleIdTokenVerifier(fetch=fetcher, clock=clock or time.monotonic)
    return verifier, fetcher


def network_error() -> Exception:
    return httpx.ConnectError("sin red")
