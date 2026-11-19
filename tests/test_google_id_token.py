"""Verificación del ID token de Google de KIZUNA en la API (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, 4.3; I1.10)."""

import time

import httpx
import jwt
import pytest

from app.core.google_id_token import (
    DEFAULT_CERTS_TTL_SECONDS,
    GOOGLE_CERTS_URL,
    GoogleIdTokenVerifier,
    IdTokenError,
    _max_age,
    fetch_google_certs,
)
from tests.google_token_helpers import AUDIENCE, EMAIL, Clock, KeyPair, claims, network_error, verifier_with

KEY = KeyPair("clave-1")


async def verify(verifier: GoogleIdTokenVerifier, token: str, audiences: list[str] | None = None) -> None:
    await verifier.verify(token, audiences=audiences or [AUDIENCE], email=EMAIL)


async def rejected(
    verifier: GoogleIdTokenVerifier, token: str, audiences: list[str] | None = None
) -> IdTokenError:
    with pytest.raises(IdTokenError) as info:
        await verify(verifier, token, audiences)
    return info.value


async def test_valid_token_is_accepted() -> None:
    verifier, _ = verifier_with(KEY)
    await verify(verifier, KEY.sign(claims()))


@pytest.mark.parametrize("issuer", ["https://accounts.google.com", "accounts.google.com"])
async def test_both_google_issuers_are_accepted(issuer: str) -> None:
    verifier, _ = verifier_with(KEY)
    await verify(verifier, KEY.sign(claims(iss=issuer)))


async def test_any_configured_audience_is_accepted() -> None:
    verifier, _ = verifier_with(KEY)
    token = KEY.sign(claims(aud="https://consent-api-kizuna-123.us-east1.run.app"))
    await verify(verifier, token, [AUDIENCE, "https://consent-api-kizuna-123.us-east1.run.app"])


async def test_email_comparison_ignores_case() -> None:
    verifier, _ = verifier_with(KEY)
    await verify(verifier, KEY.sign(claims(email=EMAIL.upper())))


async def test_small_clock_skew_is_tolerated() -> None:
    verifier, _ = verifier_with(KEY)
    await verify(verifier, KEY.sign(claims(exp=int(time.time()) - 10)))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "token vencido"),
        ({"aud": "https://otra-audiencia.example"}, "audiencia distinta"),
        ({"iss": "https://login.microsoftonline.com/x/v2.0"}, "emisor distinto de Google"),
        ({"exp": None}, "firma o claims inválidos"),
        ({"iat": None}, "firma o claims inválidos"),
    ],
)
async def test_invalid_claims_are_401(overrides: dict[str, object], reason: str) -> None:
    verifier, _ = verifier_with(KEY)
    error = await rejected(verifier, KEY.sign(claims(**overrides)))
    assert (error.status_code, error.reason) == (401, reason)


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "otra-cuenta@proyecto-de-prueba.iam.gserviceaccount.com"},
        {"email": None},
        {"email_verified": False},
        {"email_verified": None},
        {"email_verified": "true"},
    ],
)
async def test_other_identity_is_403(overrides: dict[str, object]) -> None:
    verifier, _ = verifier_with(KEY)
    error = await rejected(verifier, KEY.sign(claims(**overrides)))
    assert error.status_code == 403


async def test_token_signed_by_another_key_is_rejected() -> None:
    impostor = KeyPair("clave-1")  # mismo kid, otra clave privada
    verifier, _ = verifier_with(KEY)
    error = await rejected(verifier, impostor.sign(claims()))
    assert (error.status_code, error.reason) == (401, "firma o claims inválidos")


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
async def test_other_algorithms_are_rejected(algorithm: str) -> None:
    verifier, fetcher = verifier_with(KEY)
    key = "s" * 32 if algorithm == "HS256" else None
    token = jwt.encode(claims(), key, algorithm=algorithm, headers={"kid": KEY.kid})
    error = await rejected(verifier, token)
    assert (error.status_code, error.reason) == (401, "algoritmo no admitido")
    assert fetcher.calls == 0


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "Bearer x"])
async def test_malformed_tokens_are_401(token: str) -> None:
    verifier, _ = verifier_with(KEY)
    error = await rejected(verifier, token)
    assert error.status_code == 401


async def test_token_without_kid_is_rejected() -> None:
    verifier, _ = verifier_with(KEY)
    token = jwt.encode(claims(), KEY.private_key, algorithm="RS256")
    error = await rejected(verifier, token)
    assert error.reason == "token sin kid"


# --- Claves de Google en caché ---


async def test_keys_are_downloaded_once_and_reused() -> None:
    verifier, fetcher = verifier_with(KEY)
    for _ in range(5):
        await verify(verifier, KEY.sign(claims()))
    assert fetcher.calls == 1


async def test_keys_are_downloaded_again_when_they_expire() -> None:
    clock = Clock()
    verifier, fetcher = verifier_with(KEY, clock=clock)
    fetcher.ttl = 600
    await verify(verifier, KEY.sign(claims()))
    clock.now += 599
    await verify(verifier, KEY.sign(claims()))
    assert fetcher.calls == 1
    clock.now += 2
    await verify(verifier, KEY.sign(claims()))
    assert fetcher.calls == 2


async def test_rotated_key_is_found_with_one_extra_download() -> None:
    clock = Clock()
    rotated = KeyPair("clave-2")
    verifier, fetcher = verifier_with(KEY, clock=clock)
    await verify(verifier, KEY.sign(claims()))
    fetcher.keys.append(rotated)
    clock.now += 61
    await verify(verifier, rotated.sign(claims()))
    assert fetcher.calls == 2


async def test_unknown_kids_do_not_download_more_than_once_a_minute() -> None:
    clock = Clock()
    verifier, fetcher = verifier_with(KEY, clock=clock)
    await verify(verifier, KEY.sign(claims()))
    for kid in ("x1", "x2", "x3"):
        error = await rejected(verifier, KEY.sign(claims(), kid=kid))
        assert (error.status_code, error.reason) == (401, "clave de firma desconocida")
    assert fetcher.calls == 1
    clock.now += 60
    await rejected(verifier, KEY.sign(claims(), kid="x4"))
    assert fetcher.calls == 2


async def test_unavailable_google_keys_are_503_and_retried() -> None:
    verifier, fetcher = verifier_with(KEY)
    fetcher.error = network_error()
    error = await rejected(verifier, KEY.sign(claims()))
    assert (error.status_code, error.reason) == (503, "no se pudieron obtener las claves de Google")
    fetcher.error = None
    await verify(verifier, KEY.sign(claims()))
    assert fetcher.calls == 2


@pytest.mark.parametrize("payload", [{"keys": [{"kid": "x", "kty": "RSA"}]}, ["no", "es", "un", "dict"]])
async def test_malformed_google_keys_are_503(payload: object) -> None:
    async def fetch() -> tuple[object, float]:
        return payload, 3600.0

    verifier = GoogleIdTokenVerifier(fetch=fetch)  # type: ignore[arg-type]
    error = await rejected(verifier, KEY.sign(claims()))
    assert error.status_code == 503


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("public, max-age=19883, must-revalidate, no-transform", 19883.0),
        ("no-cache", DEFAULT_CERTS_TTL_SECONDS),
        (None, DEFAULT_CERTS_TTL_SECONDS),
    ],
)
def test_cache_lifetime_comes_from_cache_control(header: str | None, expected: float) -> None:
    assert _max_age(header) == expected


async def test_fetch_google_certs_reads_the_jwks_and_its_lifetime() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, json={"keys": [KEY.jwk()]}, headers={"Cache-Control": "public, max-age=120"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jwks, ttl = await fetch_google_certs(client)
    assert seen == [GOOGLE_CERTS_URL]
    assert jwks["keys"][0]["kid"] == KEY.kid
    assert ttl == 120.0


async def test_fetch_google_certs_raises_on_http_errors() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_google_certs(client)
