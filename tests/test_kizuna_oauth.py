"""K1: OAuth 2.0 del callback a KIZUNA (INTEGRACION_KIZUNA_OAUTH2.spec.md, secciones 4, 5 y 6).

El mock de KIZUNA (token endpoint al estilo Entra ID + callback) se usa como transporte ASGI, sin red. Los casos que
el mock no simula (respuestas raras del token endpoint) usan un httpx.MockTransport que enruta por ruta.
"""

import base64
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.integrations.kizuna_client import KizunaConfigurationError, check_kizuna_settings
from app.integrations.kizuna_oauth import KizunaTokenError, KizunaTokenProvider, get_kizuna_token_provider
from app.models import KizunaNotification, NotificationStatus
from app.services.kizuna_notifications import KizunaNotifier
from mocks.kizuna_mock import main as kizuna_mock
from tests.db_helpers import Sessions
from tests.test_kizuna_notifications import create_many, create_notification, get_notification

MOCK = "http://kizuna-mock:8081"
CALLBACK_URL = f"{MOCK}/kizuna/consent-result"
TOKEN_URL = f"{MOCK}/kizuna/oauth/token"
SCOPE = "api://kizuna-mock/.default"
SECRET = "mock-secret"


@pytest.fixture(autouse=True)
def oauth_settings(test_settings: Settings) -> Settings:
    test_settings.kizuna_callback_enabled = True
    test_settings.kizuna_callback_url = CALLBACK_URL
    test_settings.kizuna_callback_max_retries = 3
    test_settings.kizuna_callback_backoff_seconds = [5, 30, 120]
    test_settings.kizuna_callback_timeout_seconds = 10
    test_settings.kizuna_callback_auth_mode = "oauth2"
    test_settings.kizuna_oauth_token_url = TOKEN_URL
    test_settings.kizuna_oauth_client_id = "mock-client"
    test_settings.kizuna_oauth_client_secret = SecretStr(SECRET)
    test_settings.kizuna_oauth_scope = SCOPE
    test_settings.kizuna_oauth_client_auth_method = "client_secret_post"
    test_settings.kizuna_oauth_refresh_margin_seconds = 300
    return test_settings


@pytest.fixture(autouse=True)
def mock_state(monkeypatch: pytest.MonkeyPatch) -> kizuna_mock.MockState:
    """Mock de KIZUNA limpio, sin fallos simulados y exigiendo Bearer."""
    for name in (
        "MOCK_FAIL_TIMES",
        "MOCK_RESPONSE_DELAY_SECONDS",
        "MOCK_CLIENT_ID",
        "MOCK_CLIENT_SECRET",
        "MOCK_TOKEN_EXPIRES_IN",
        "MOCK_TOKEN_FAIL_TIMES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_REQUIRE_AUTH", "true")
    state = kizuna_mock.state
    state.total_requests = 0
    state.token_requests = 0
    state.received.clear()
    state.tokens.clear()
    return state


class RecordingTransport(httpx.AsyncBaseTransport):
    """Deja pasar las peticiones al transporte real y las guarda."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)

    def to(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport(httpx.ASGITransport(app=kizuna_mock.app))


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_notifier(
    oauth_settings: Settings, session_factory: Sessions, sleeps: list[float], transport: RecordingTransport
) -> Callable[..., KizunaNotifier]:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def factory(custom: httpx.AsyncBaseTransport | None = None) -> KizunaNotifier:
        return KizunaNotifier(
            oauth_settings, session_factory, transport=custom or transport, sleep=record_sleep
        )

    return factory


def routed_transport(
    token: Callable[[httpx.Request], httpx.Response],
    callback: Callable[[httpx.Request], httpx.Response] | None = None,
) -> RecordingTransport:
    """Token endpoint y callback simulados por función (lo que el mock no cubre)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kizuna/oauth/token":
            return token(request)
        return (callback or (lambda _: httpx.Response(200, json={"received": True})))(request)

    return RecordingTransport(httpx.MockTransport(handler))


def token_ok(**overrides: Any) -> Callable[[httpx.Request], httpx.Response]:
    body = {"token_type": "Bearer", "expires_in": 3599, "access_token": "token-1", **overrides}
    return lambda _: httpx.Response(200, json={k: v for k, v in body.items() if v is not None})


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


async def send_one(
    sessions: Sessions,
    settings: Settings,
    notifier: KizunaNotifier,
    transaction_id: str = "TRX-1",
    customer_id: str = "12345678",
) -> KizunaNotification:
    notification_id = await create_notification(
        sessions, settings, transaction_id=transaction_id, customer_id=customer_id
    )
    await notifier.dispatch([notification_id])
    return await get_notification(sessions, notification_id)


# --- Envío con Bearer (4.1–4.3) ---


async def test_callback_carries_the_bearer_issued_by_the_token_endpoint(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    transport: RecordingTransport,
    mock_state: kizuna_mock.MockState,
) -> None:
    notification = await send_one(session_factory, oauth_settings, make_notifier())

    assert (notification.status, notification.attempts) == (NotificationStatus.ENVIADO, 1)
    assert mock_state.token_requests == 1
    assert [item["authenticated"] for item in mock_state.received] == [True]
    (callback,) = transport.to("/kizuna/consent-result")
    (issued,) = mock_state.tokens
    assert callback.headers["Authorization"] == f"Bearer {issued}"


async def test_client_secret_post_sends_the_credentials_in_the_body(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    transport: RecordingTransport,
) -> None:
    await send_one(session_factory, oauth_settings, make_notifier())

    (request,) = transport.to("/kizuna/oauth/token")
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert "Authorization" not in request.headers
    assert form(request) == {
        "grant_type": "client_credentials",
        "scope": SCOPE,
        "client_id": "mock-client",
        "client_secret": SECRET,
    }


async def test_client_secret_basic_sends_the_credentials_in_the_header(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Caracteres que RFC 6749 §2.3.1 obliga a codificar antes del Base64.
    monkeypatch.setenv("MOCK_CLIENT_SECRET", "s3cr:et con+espacios~")
    oauth_settings.kizuna_oauth_client_secret = SecretStr("s3cr:et con+espacios~")
    oauth_settings.kizuna_oauth_client_auth_method = "client_secret_basic"

    notification = await send_one(session_factory, oauth_settings, make_notifier())

    assert notification.status == NotificationStatus.ENVIADO
    (request,) = transport.to("/kizuna/oauth/token")
    scheme, encoded = request.headers["Authorization"].split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "mock-client:s3cr%3Aet+con%2Bespacios~"
    assert form(request) == {"grant_type": "client_credentials", "scope": SCOPE}


# --- Caché (4.2) ---


async def test_the_token_is_reused_between_sends(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    await send_one(session_factory, oauth_settings, make_notifier(), "TRX-1", "10000001")
    await send_one(session_factory, oauth_settings, make_notifier(), "TRX-2", "10000002")

    assert mock_state.token_requests == 1
    assert [item["authenticated"] for item in mock_state.received] == [True, True]


async def test_ten_concurrent_sends_request_a_single_token(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    await create_many(session_factory, oauth_settings, 10)

    assert await make_notifier().reprocess_pending() == 10

    assert mock_state.token_requests == 1
    assert len(mock_state.received) == 10


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def token_requests_after(
    oauth_settings: Settings, token: Callable[[httpx.Request], httpx.Response], moments: list[float]
) -> int:
    """Pide un token en cada instante (segundos desde el primero) y cuenta los pedidos al token endpoint."""
    clock = Clock()
    provider = KizunaTokenProvider(clock=clock)
    transport = routed_transport(token)
    start = clock.now
    async with httpx.AsyncClient(transport=transport) as client:
        for moment in moments:
            clock.now = start + moment
            await provider.get_token(client, oauth_settings)
    return len(transport.to("/kizuna/oauth/token"))


async def test_the_token_is_renewed_at_the_refresh_margin(oauth_settings: Settings) -> None:
    # expires_in 3600 − margen 300 = vigente 3300 s.
    token = token_ok(expires_in=3600)
    assert await token_requests_after(oauth_settings, token, [0, 3299.9]) == 1
    assert await token_requests_after(oauth_settings, token, [0, 3300]) == 2


async def test_the_refresh_margin_is_configurable(oauth_settings: Settings) -> None:
    oauth_settings.kizuna_oauth_refresh_margin_seconds = 0
    assert await token_requests_after(oauth_settings, token_ok(expires_in=3600), [0, 3599]) == 1


async def test_expires_in_as_numeric_text_is_accepted(oauth_settings: Settings) -> None:
    assert await token_requests_after(oauth_settings, token_ok(expires_in="3600"), [0, 10]) == 1


@pytest.mark.parametrize("expires_in", [None, "pronto", 0, -5, True])
async def test_a_token_without_valid_expires_in_is_used_once(
    oauth_settings: Settings, expires_in: Any
) -> None:
    assert await token_requests_after(oauth_settings, token_ok(expires_in=expires_in), [0, 1]) == 2


async def test_a_change_of_credentials_does_not_reuse_the_token(oauth_settings: Settings) -> None:
    provider = KizunaTokenProvider()
    transport = routed_transport(token_ok())
    async with httpx.AsyncClient(transport=transport) as client:
        await provider.get_token(client, oauth_settings)
        oauth_settings.kizuna_oauth_scope = "api://otro/.default"
        await provider.get_token(client, oauth_settings)
    assert len(transport.to("/kizuna/oauth/token")) == 2


# --- 401 del callback: renovar y reenviar una vez (4.4) ---


async def test_callback_401_renews_the_token_and_resends_once(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
    sleeps: list[float],
) -> None:
    await send_one(session_factory, oauth_settings, make_notifier(), "TRX-1", "10000001")
    # KIZUNA revoca el token que la API tiene en caché.
    mock_state.tokens.clear()

    notification = await send_one(session_factory, oauth_settings, make_notifier(), "TRX-2", "10000002")

    assert (notification.status, notification.attempts) == (NotificationStatus.ENVIADO, 1)
    assert mock_state.token_requests == 2
    assert mock_state.total_requests == 3  # 1 + (401 + reenvío)
    assert sleeps == []


async def test_a_second_401_ends_fallido(
    session_factory: Sessions, oauth_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    transport = routed_transport(token_ok(), lambda _: httpx.Response(401, json={"error": "invalid_token"}))

    notification = await send_one(session_factory, oauth_settings, make_notifier(transport))

    assert (notification.status, notification.attempts) == (NotificationStatus.FALLIDO, 1)
    assert notification.last_http_status == 401
    assert len(transport.to("/kizuna/oauth/token")) == 2
    assert len(transport.to("/kizuna/consent-result")) == 2


async def test_callback_403_does_not_renew_the_token(
    session_factory: Sessions, oauth_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    transport = routed_transport(token_ok(), lambda _: httpx.Response(403, json={"error": "forbidden"}))

    notification = await send_one(session_factory, oauth_settings, make_notifier(transport))

    assert (notification.status, notification.attempts) == (NotificationStatus.FALLIDO, 1)
    assert len(transport.to("/kizuna/oauth/token")) == 1
    assert len(transport.to("/kizuna/consent-result")) == 1


# --- Errores del token endpoint (4.4) ---


async def test_invalid_client_ends_fallido_without_calling_the_callback(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
    sleeps: list[float],
) -> None:
    oauth_settings.kizuna_oauth_client_secret = SecretStr("secreto-equivocado")

    notification = await send_one(session_factory, oauth_settings, make_notifier())

    assert (notification.status, notification.attempts) == (NotificationStatus.FALLIDO, 1)
    assert notification.last_http_status == 401
    assert notification.last_error == "Token: HTTP 401: invalid_client: Credenciales del cliente inválidas"
    assert mock_state.total_requests == 0
    assert sleeps == []


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (400, {"error": "invalid_scope", "error_description": "AADSTS70011: scope inválido"}),
        (400, {"error": "unauthorized_client"}),
        (403, {}),
        (404, "no es json"),
        (302, {}),
    ],
)
async def test_other_token_errors_end_fallido(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    status_code: int,
    body: Any,
) -> None:
    def token(_: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status_code, text=body)
        return httpx.Response(status_code, json=body)

    transport = routed_transport(token)
    notification = await send_one(session_factory, oauth_settings, make_notifier(transport))

    assert (notification.status, notification.attempts) == (NotificationStatus.FALLIDO, 1)
    assert notification.last_http_status == status_code
    details = [
        str(body[key]) for key in ("error", "error_description") if isinstance(body, dict) and key in body
    ]
    assert notification.last_error == ": ".join(["Token: HTTP " + str(status_code), *details])
    assert transport.to("/kizuna/consent-result") == []


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"token_type": "Bearer", "expires_in": 3599}, "Token: Respuesta sin access_token"),
        ({"token_type": "Bearer", "access_token": ""}, "Token: Respuesta sin access_token"),
        ({"token_type": "mac", "access_token": "x"}, "Token: Respuesta con token_type distinto de Bearer"),
        ({"access_token": "x"}, "Token: Respuesta con token_type distinto de Bearer"),
        (["no", "es", "objeto"], "Token: Respuesta sin access_token"),
    ],
)
async def test_an_invalid_token_response_ends_fallido(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    body: Any,
    error: str,
) -> None:
    transport = routed_transport(lambda _: httpx.Response(200, json=body))

    notification = await send_one(session_factory, oauth_settings, make_notifier(transport))

    assert (notification.status, notification.attempts, notification.last_error) == (
        NotificationStatus.FALLIDO,
        1,
        error,
    )
    assert transport.to("/kizuna/consent-result") == []


async def test_token_type_is_case_insensitive(
    session_factory: Sessions, oauth_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    transport = routed_transport(token_ok(token_type="bearer"))
    notification = await send_one(session_factory, oauth_settings, make_notifier(transport))
    assert notification.status == NotificationStatus.ENVIADO


async def test_token_5xx_is_retried_with_backoff(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
) -> None:
    monkeypatch.setenv("MOCK_TOKEN_FAIL_TIMES", "2")

    notification = await send_one(session_factory, oauth_settings, make_notifier())

    assert (notification.status, notification.attempts) == (NotificationStatus.ENVIADO, 3)
    assert sleeps == [5, 30]
    assert mock_state.token_requests == 3


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429, json={"error": "too_many_requests"}),
        httpx.ConnectError("sin conexión"),
        httpx.ReadTimeout("lento"),
    ],
)
async def test_token_429_and_network_errors_are_retried(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    sleeps: list[float],
    failure: httpx.Response | Exception,
) -> None:
    calls: list[int] = []

    def token(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return token_ok()(request)

    notification = await send_one(session_factory, oauth_settings, make_notifier(routed_transport(token)))

    assert (notification.status, notification.attempts) == (NotificationStatus.ENVIADO, 2)
    assert sleeps == [5]


async def test_token_errors_exhaust_the_retries(
    session_factory: Sessions, oauth_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    transport = routed_transport(lambda _: httpx.Response(503, json={"error": "temporarily_unavailable"}))

    notification = await send_one(session_factory, oauth_settings, make_notifier(transport))

    assert (notification.status, notification.attempts) == (NotificationStatus.FALLIDO, 4)
    assert notification.last_error == "Token: HTTP 503: temporarily_unavailable"


async def test_provider_raises_token_error_with_status(oauth_settings: Settings) -> None:
    async with httpx.AsyncClient(transport=routed_transport(lambda _: httpx.Response(401))) as client:
        with pytest.raises(KizunaTokenError) as raised:
            await KizunaTokenProvider().get_token(client, oauth_settings)
    assert (raised.value.retryable, raised.value.http_status, raised.value.error) == (False, 401, "HTTP 401")


# --- Secretos (4.5) ---


async def test_secret_and_tokens_are_never_logged_or_stored(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def echoing_token(_: httpx.Request) -> httpx.Response:
        # Un servidor que repite el secreto en su error: no debe llegar a last_error.
        return httpx.Response(
            401, json={"error": "invalid_client", "error_description": f"secreto {SECRET} malo"}
        )

    with caplog.at_level(logging.DEBUG):
        ok = await send_one(session_factory, oauth_settings, make_notifier(), "TRX-1", "10000001")
        # Sin el token en caché: el segundo envío debe pedir uno nuevo al servidor que falla.
        get_kizuna_token_provider.cache_clear()
        failed = await send_one(
            session_factory,
            oauth_settings,
            make_notifier(routed_transport(echoing_token)),
            "TRX-2",
            "10000002",
        )

    assert ok.status == NotificationStatus.ENVIADO
    assert failed.last_error == "Token: HTTP 401: invalid_client: secreto **** malo"
    (issued,) = mock_state.tokens
    # Los loggers "httpx" y "kizuna_mock" no son de la aplicación (cliente de prueba y mock).
    logged = "\n".join(
        f"{r.getMessage()} {json.dumps(r.__dict__, default=str)}"
        for r in caplog.records
        if r.name not in ("httpx", "kizuna_mock")
    )
    assert "Token de KIZUNA obtenido" in logged
    for secret in (SECRET, issued, "Bearer"):
        assert secret not in logged
    for notification in (ok, failed):
        assert SECRET not in (notification.last_error or "")
        assert issued not in (notification.last_error or "")


async def test_header_mode_never_requests_a_token(
    session_factory: Sessions,
    oauth_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_REQUIRE_AUTH", "false")
    oauth_settings.kizuna_callback_auth_mode = "header"

    notification = await send_one(session_factory, oauth_settings, make_notifier())

    assert notification.status == NotificationStatus.ENVIADO
    assert transport.to("/kizuna/oauth/token") == []
    (callback,) = transport.to("/kizuna/consent-result")
    assert "Authorization" not in callback.headers


# --- Configuración (4.6 y 5) ---


def settings_with(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "kizuna_callback_enabled": True,
        "kizuna_callback_url": CALLBACK_URL,
        "kizuna_callback_auth_mode": "oauth2",
        "kizuna_oauth_token_url": TOKEN_URL,
        "kizuna_oauth_client_id": "mock-client",
        "kizuna_oauth_client_secret": SECRET,
        "kizuna_oauth_scope": SCOPE,
    }
    return Settings(_env_file=None, database_url="postgresql+asyncpg://x", **{**base, **values})


def test_startup_ok_with_complete_oauth2_settings(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.kizuna_client"):
        check_kizuna_settings(settings_with())
    assert caplog.records == []


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"kizuna_oauth_token_url": ""}, "falta KIZUNA_OAUTH_TOKEN_URL"),
        ({"kizuna_oauth_client_id": "  "}, "falta KIZUNA_OAUTH_CLIENT_ID"),
        ({"kizuna_oauth_client_secret": ""}, "falta KIZUNA_OAUTH_CLIENT_SECRET"),
        ({"kizuna_oauth_scope": ""}, "falta KIZUNA_OAUTH_SCOPE"),
        ({"kizuna_callback_auth_header": "X-Kizuna-Key"}, "KIZUNA_CALLBACK_AUTH_HEADER debe quedar vacío"),
        (
            {"app_env": "production", "kizuna_oauth_token_url": "https://login.example/token"},
            "KIZUNA_CALLBACK_URL debe ser https",
        ),
        (
            {"app_env": "production", "kizuna_callback_url": "https://kizuna.example/callback"},
            "KIZUNA_OAUTH_TOKEN_URL debe ser https",
        ),
        (
            {"kizuna_callback_auth_mode": "header", "app_env": "production"},
            "KIZUNA_CALLBACK_URL debe ser https",
        ),
    ],
)
def test_startup_fails_with_invalid_oauth2_settings(values: dict[str, Any], problem: str) -> None:
    with pytest.raises(KizunaConfigurationError, match=problem):
        check_kizuna_settings(settings_with(**values))


def test_startup_ok_in_production_with_https(caplog: pytest.LogCaptureFixture) -> None:
    settings = settings_with(
        app_env="production",
        kizuna_callback_url="https://kizuna.example/api/services/x",
        kizuna_oauth_token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
    )
    with caplog.at_level(logging.WARNING, logger="app.integrations.kizuna_client"):
        check_kizuna_settings(settings)
    assert caplog.records == []


def test_disabled_callback_skips_the_oauth2_checks() -> None:
    check_kizuna_settings(settings_with(kizuna_callback_enabled=False, kizuna_oauth_token_url=""))


def test_header_mode_ignores_the_oauth2_settings() -> None:
    check_kizuna_settings(settings_with(kizuna_callback_auth_mode="header", kizuna_oauth_token_url=""))


def test_mode_and_method_are_case_insensitive() -> None:
    settings = settings_with(
        kizuna_callback_auth_mode=" OAuth2 ", kizuna_oauth_client_auth_method="CLIENT_SECRET_BASIC"
    )
    assert (settings.kizuna_callback_auth_mode, settings.kizuna_oauth_client_auth_method) == (
        "oauth2",
        "client_secret_basic",
    )


@pytest.mark.parametrize(
    "values",
    [
        {"kizuna_callback_auth_mode": "none"},
        {"kizuna_oauth_client_auth_method": "private_key_jwt"},
        {"kizuna_oauth_refresh_margin_seconds": -1},
        {"kizuna_oauth_refresh_margin_seconds": 1801},
    ],
)
def test_invalid_oauth2_values_are_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        settings_with(**values)


def test_defaults_keep_the_m5_behavior() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://x")
    assert settings.kizuna_callback_auth_mode == "header"
    assert settings.kizuna_oauth_client_auth_method == "client_secret_post"
    assert settings.kizuna_oauth_refresh_margin_seconds == 300


def test_oauth2_values_are_trimmed() -> None:
    settings = settings_with(kizuna_oauth_token_url=f" {TOKEN_URL} ", kizuna_oauth_scope=f" {SCOPE}\n")
    assert (settings.kizuna_oauth_token_url, settings.kizuna_oauth_scope) == (TOKEN_URL, SCOPE)
