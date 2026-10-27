"""M5: notificación a KIZUNA (outbox, envío después del commit, reintentos, interruptor y reproceso al arrancar).

El mock de KIZUNA (mocks/kizuna_mock) se usa como transporte ASGI: mismo código que en docker compose, sin red.
Las esperas entre reintentos se registran en lugar de dormir.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.core.config import Settings
from app.integrations.kizuna_client import (
    KizunaClient,
    KizunaConfigurationError,
    check_kizuna_settings,
)
from app.models import Consent, ConsentStatus, DocumentType, KizunaNotification, NotificationStatus
from app.services.kizuna_notifications import (
    REPROCESS_CONCURRENCY,
    KizunaNotifier,
    backoff_seconds,
    enqueue_notifications,
    reprocess_pending_in_background,
)
from mocks.kizuna_mock import main as kizuna_mock
from tests.conftest import RecordingWhatsAppSender
from tests.db_helpers import Sessions, all_rows, count, get_consent

CALLBACK_URL = "http://kizuna-mock:8081/kizuna/consent-result"
API_KEY_HEADER = {"X-API-Key": "test-api-key"}
BODY: dict[str, Any] = {
    "transaction_id": "TRX-1",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan pérez",
    "phone": "987654321",
    "dealer": "DEALER-001",
}
ACCEPT = {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}
REJECT = {"personal_data": False, "marketing_yamaha": True, "marketing_dealer": True}


@pytest.fixture(autouse=True)
def kizuna_settings(test_settings: Settings) -> Settings:
    test_settings.kizuna_callback_enabled = True
    test_settings.kizuna_callback_url = CALLBACK_URL
    test_settings.kizuna_callback_max_retries = 3
    test_settings.kizuna_callback_backoff_seconds = [5, 30, 120]
    test_settings.kizuna_callback_timeout_seconds = 10
    return test_settings


@pytest.fixture(autouse=True)
def mock_state(monkeypatch: pytest.MonkeyPatch) -> kizuna_mock.MockState:
    """Mock de KIZUNA limpio y sin fallos simulados."""
    monkeypatch.delenv("MOCK_FAIL_TIMES", raising=False)
    monkeypatch.delenv("MOCK_RESPONSE_DELAY_SECONDS", raising=False)
    kizuna_mock.state.total_requests = 0
    kizuna_mock.state.received.clear()
    return kizuna_mock.state


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_notifier(
    test_settings: Settings, session_factory: Sessions, sleeps: list[float]
) -> Callable[..., KizunaNotifier]:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def factory(transport: httpx.AsyncBaseTransport | None = None) -> KizunaNotifier:
        return KizunaNotifier(
            test_settings,
            session_factory,
            transport=transport or httpx.ASGITransport(app=kizuna_mock.app),
            sleep=record_sleep,
        )

    return factory


@pytest.fixture
def use_transport(
    api_client: httpx.AsyncClient, make_notifier: Callable[..., KizunaNotifier]
) -> Iterator[Callable[[httpx.AsyncBaseTransport], None]]:
    """La API usa un KizunaNotifier contra el mock (por defecto) o contra el transporte indicado."""
    from app.main import app
    from app.services.kizuna_notifications import get_kizuna_notifier

    def set_transport(transport: httpx.AsyncBaseTransport) -> None:
        notifier = make_notifier(transport)
        app.dependency_overrides[get_kizuna_notifier] = lambda: notifier

    set_transport(httpx.ASGITransport(app=kizuna_mock.app))
    try:
        yield set_transport
    finally:
        app.dependency_overrides.pop(get_kizuna_notifier, None)


async def register(
    client: httpx.AsyncClient, sender: RecordingWhatsAppSender, **overrides: Any
) -> httpx.Response:
    response = await client.post("/api/v1/consents", json={**BODY, **overrides}, headers=API_KEY_HEADER)
    assert response.status_code in (200, 201), response.text
    return response


async def submit(client: httpx.AsyncClient, token: str, answers: dict[str, bool]) -> httpx.Response:
    # La landing no envía API Key.
    return await client.post(
        f"/api/v1/public/consent-links/{token}/submit", json=answers, headers={"X-API-Key": ""}
    )


async def answered(
    client: httpx.AsyncClient, sender: RecordingWhatsAppSender, answers: dict[str, bool]
) -> None:
    await register(client, sender)
    response = await submit(client, sender.calls[-1]["token"], answers)
    assert response.status_code == 200, response.text


async def notifications(sessions: Sessions) -> dict[str, KizunaNotification]:
    return {n.transaction_id: n for n in await all_rows(sessions, KizunaNotification)}


async def create_notification(
    sessions: Sessions,
    settings: Settings,
    transaction_id: str = "TRX-1",
    document_number: str = "12345678",
) -> uuid.UUID:
    """Registro ACEPTADO con su notificación PENDIENTE, sin pasar por la API."""
    async with sessions.begin() as session:
        consent = Consent(
            transaction_id=transaction_id,
            document_type=DocumentType.DNI,
            document_number=document_number,
            dealer="DEALER-001",
            full_name="JUAN PÉREZ",
            phone="987654321",
            status=ConsentStatus.ACEPTADO,
            consent_personal_data=True,
            consent_marketing_yamaha=False,
            consent_marketing_dealer=True,
            responded_at=datetime(2026, 11, 5, 15, 20, 11, tzinfo=UTC),
            is_deleted=0,
        )
        session.add(consent)
        await session.flush()
        (notification_id,) = await enqueue_notifications(session, [consent], settings=settings)
    return notification_id


def sequence_transport(
    responses: list[int | Exception], seen: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    """Responde con los códigos (o lanza las excepciones) en orden; luego repite el último."""
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        item = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={"received": item < 300})

    return httpx.MockTransport(handler)


# --- Criterios de aceptación M5 (contra el mock) ---


async def test_mock_fail_times_3_ends_enviado_with_4_attempts(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_FAIL_TIMES", "3")
    await answered(api_client, whatsapp_sender, ACCEPT)

    notification = (await notifications(session_factory))["TRX-1"]
    assert notification.status == NotificationStatus.ENVIADO
    assert notification.attempts == 4
    assert (notification.last_http_status, notification.last_error) == (200, None)
    assert notification.sent_at is not None
    assert notification.last_attempt_at == notification.sent_at
    # 1 intento + 3 reintentos, con la espera configurada antes de cada reintento.
    assert sleeps == [5, 30, 120]
    assert mock_state.total_requests == 4
    (received,) = mock_state.received
    assert received["payload"] == notification.payload


async def test_mock_failing_always_ends_fallido(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MOCK_FAIL_TIMES", "1000")
    with caplog.at_level(logging.WARNING, logger="app.services.kizuna_notifications"):
        await answered(api_client, whatsapp_sender, ACCEPT)

    notification = (await notifications(session_factory))["TRX-1"]
    assert notification.status == NotificationStatus.FALLIDO
    assert notification.attempts == 4
    assert notification.last_http_status == 500
    assert notification.last_error.startswith("HTTP 500")
    assert notification.sent_at is None
    assert sleeps == [5, 30, 120]
    assert mock_state.total_requests == 4
    assert mock_state.received == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "No se pudo enviar la respuesta a KIZUNA: se agotaron los reintentos"
    ]


async def test_callback_disabled_sends_nothing(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
    kizuna_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    kizuna_settings.kizuna_callback_enabled = False
    with caplog.at_level(logging.INFO, logger="app.services.kizuna_notifications"):
        await answered(api_client, whatsapp_sender, ACCEPT)

    notification = (await notifications(session_factory))["TRX-1"]
    assert (notification.status, notification.attempts, notification.last_attempt_at) == (
        NotificationStatus.PENDIENTE,
        0,
        None,
    )
    assert mock_state.total_requests == 0
    assert any("KIZUNA_CALLBACK_ENABLED=false" in r.getMessage() for r in caplog.records)


# --- Envío después del commit, conectado al submit ---


async def test_submit_sends_the_answer_to_kizuna(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
    sleeps: list[float],
) -> None:
    await answered(api_client, whatsapp_sender, ACCEPT)

    notification = (await notifications(session_factory))["TRX-1"]
    assert (notification.status, notification.attempts, notification.last_http_status) == (
        NotificationStatus.ENVIADO,
        1,
        200,
    )
    assert sleeps == []
    consent = await get_consent(session_factory, "TRX-1")
    assert mock_state.received[0]["payload"] == {
        "transaction_id": "TRX-1",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "DEALER-001",
        "status": "ACEPTADO",
        "consents": {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
        "responded_at": consent.responded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


async def test_rejection_reaches_kizuna_with_marketing_null(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
) -> None:
    await answered(api_client, whatsapp_sender, REJECT)
    (received,) = mock_state.received
    assert received["payload"]["status"] == "NO_ACEPTADO"
    assert received["payload"]["consents"] == {
        "personal_data": False,
        "marketing_yamaha": None,
        "marketing_dealer": None,
    }


async def test_kizuna_failure_does_not_affect_the_customer_response(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
) -> None:
    use_transport(sequence_transport([httpx.ConnectError("sin conexión")]))
    await register(api_client, whatsapp_sender)
    response = await submit(api_client, whatsapp_sender.calls[-1]["token"], ACCEPT)

    assert response.status_code == 200
    assert response.json() == {"status": "ACEPTADO"}
    assert (await get_consent(session_factory, "TRX-1")).status == ConsentStatus.ACEPTADO
    assert (await notifications(session_factory))["TRX-1"].status == NotificationStatus.FALLIDO


# --- RN-04: una respuesta por cada ID de transacción ---


async def test_each_transaction_id_gets_its_own_callback(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
) -> None:
    await register(api_client, whatsapp_sender)
    token = whatsapp_sender.calls[-1]["token"]
    await register(api_client, whatsapp_sender, transaction_id="TRX-2")
    await register(api_client, whatsapp_sender, transaction_id="TRX-3")
    await submit(api_client, token, ACCEPT)

    received_ids = sorted(item["payload"]["transaction_id"] for item in mock_state.received)
    assert received_ids == ["TRX-1", "TRX-2", "TRX-3"]
    assert {item["payload"]["status"] for item in mock_state.received} == {"ACEPTADO"}
    stored = await notifications(session_factory)
    assert {tid: n.status for tid, n in stored.items()} == {
        "TRX-1": NotificationStatus.ENVIADO,
        "TRX-2": NotificationStatus.ENVIADO,
        "TRX-3": NotificationStatus.ENVIADO,
    }


async def test_retries_are_independent_per_transaction_id(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # KIZUNA rechaza siempre TRX-2 con 500; TRX-1 se recibe al primer intento.
        failing = b'"TRX-2"' in request.content
        return httpx.Response(500 if failing else 200)

    use_transport(httpx.MockTransport(handler))
    await register(api_client, whatsapp_sender)
    token = whatsapp_sender.calls[-1]["token"]
    await register(api_client, whatsapp_sender, transaction_id="TRX-2")
    await submit(api_client, token, ACCEPT)

    stored = await notifications(session_factory)
    assert (stored["TRX-1"].status, stored["TRX-1"].attempts) == (NotificationStatus.ENVIADO, 1)
    assert (stored["TRX-2"].status, stored["TRX-2"].attempts) == (NotificationStatus.FALLIDO, 4)


async def test_copy_born_answered_is_notified_by_post_consents(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
) -> None:
    await answered(api_client, whatsapp_sender, ACCEPT)
    response = await register(api_client, whatsapp_sender, transaction_id="TRX-2")
    assert response.status_code == 200
    assert response.json()["status"] == "ACEPTADO"

    # KIZUNA recibe la respuesta del ID nuevo aunque el cliente no vuelva a responder, con su propio ID.
    stored = await notifications(session_factory)
    assert stored["TRX-2"].status == NotificationStatus.ENVIADO
    primary = await get_consent(session_factory, "TRX-1")
    copy_payload = next(
        i["payload"] for i in mock_state.received if i["payload"]["transaction_id"] == "TRX-2"
    )
    assert copy_payload["status"] == "ACEPTADO"
    assert copy_payload["consents"] == {
        "personal_data": True,
        "marketing_yamaha": False,
        "marketing_dealer": True,
    }
    assert copy_payload["responded_at"] == primary.responded_at.astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )


async def test_pending_copy_is_not_notified_on_creation(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
    mock_state: kizuna_mock.MockState,
) -> None:
    await register(api_client, whatsapp_sender)
    await register(api_client, whatsapp_sender, transaction_id="TRX-2")
    # Todavía no hay respuesta: la copia se notifica cuando el cliente conteste (paso 7 y 8 del submit).
    assert await count(session_factory, KizunaNotification) == 0
    assert mock_state.total_requests == 0


async def test_resending_the_same_transaction_is_not_notified(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
    use_transport: Callable,
) -> None:
    await answered(api_client, whatsapp_sender, ACCEPT)
    await register(api_client, whatsapp_sender, phone="911222333")
    # Solo la notificación del submit; el reenvío del mismo ID (RN-04, paso 2) no crea otra.
    assert await count(session_factory, KizunaNotification) == 1


# --- Reintentos: qué se reintenta y qué no ---


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
async def test_other_4xx_fail_without_retry(
    session_factory: Sessions,
    test_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    sleeps: list[float],
    status_code: int,
) -> None:
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([status_code])).dispatch([notification_id])

    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert (notification.status, notification.attempts, notification.last_http_status) == (
        NotificationStatus.FALLIDO,
        1,
        status_code,
    )
    assert sleeps == []


async def test_429_and_5xx_are_retried(
    session_factory: Sessions, test_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([429, 503, 502, 201])).dispatch([notification_id])

    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert (notification.status, notification.attempts, notification.last_http_status) == (
        NotificationStatus.ENVIADO,
        4,
        201,
    )


async def test_network_errors_are_retried(
    session_factory: Sessions, test_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([httpx.ConnectError("sin conexión")])).dispatch([notification_id])

    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert (notification.status, notification.attempts, notification.last_http_status) == (
        NotificationStatus.FALLIDO,
        4,
        None,
    )
    assert notification.last_error == "ConnectError: sin conexión"


async def test_timeout_is_retried(
    session_factory: Sessions,
    test_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El mock tarda más que KIZUNA_CALLBACK_TIMEOUT_SECONDS.
    monkeypatch.setenv("MOCK_RESPONSE_DELAY_SECONDS", "0.5")
    test_settings.kizuna_callback_timeout_seconds = 0.05
    test_settings.kizuna_callback_max_retries = 1
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier().dispatch([notification_id])

    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert (notification.status, notification.attempts, notification.last_http_status) == (
        NotificationStatus.FALLIDO,
        2,
        None,
    )
    assert notification.last_error == "Timeout: sin respuesta en 0.05 s"


@pytest.mark.parametrize(("max_retries", "expected_attempts"), [(0, 1), (1, 2), (5, 6)])
async def test_max_retries_is_configurable(
    session_factory: Sessions,
    test_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    sleeps: list[float],
    max_retries: int,
    expected_attempts: int,
) -> None:
    test_settings.kizuna_callback_max_retries = max_retries
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([500])).dispatch([notification_id])

    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert (notification.status, notification.attempts) == (NotificationStatus.FALLIDO, expected_attempts)
    # Con 5 reintentos y 3 esperas configuradas, se repite la última.
    assert sleeps == [5, 30, 120, 120, 120][:max_retries]


async def test_backoff_is_configurable(
    session_factory: Sessions,
    test_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    sleeps: list[float],
) -> None:
    test_settings.kizuna_callback_backoff_seconds = [1, 2.5, 4]
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([500])).dispatch([notification_id])
    assert sleeps == [1, 2.5, 4]


def test_backoff_seconds() -> None:
    assert [backoff_seconds(n, [5, 30, 120]) for n in (1, 2, 3, 4)] == [5, 30, 120, 120]
    assert backoff_seconds(1, []) == 0


async def test_each_attempt_is_saved_before_waiting(
    session_factory: Sessions, test_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    seen: list[tuple[int, int | None]] = []

    async def check_saved(seconds: float) -> None:
        # Durante la espera, otra sesión ya ve el intento guardado.
        async with session_factory() as session:
            notification = await session.get(KizunaNotification, notification_id)
            seen.append((notification.attempts, notification.last_http_status))

    notification_id = await create_notification(session_factory, test_settings)
    notifier = make_notifier(sequence_transport([500, 500, 200]))
    notifier._sleep = check_saved
    await notifier.dispatch([notification_id])
    assert seen == [(1, 500), (2, 500)]


async def test_notification_already_sent_is_not_sent_again(
    session_factory: Sessions, test_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    requests: list[httpx.Request] = []
    notification_id = await create_notification(session_factory, test_settings)
    notifier = make_notifier(sequence_transport([200], seen=requests))
    await notifier.dispatch([notification_id])
    await notifier.dispatch([notification_id])
    assert len(requests) == 1


async def test_request_goes_to_target_url_with_auth_header(
    session_factory: Sessions, test_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    test_settings.kizuna_callback_auth_header = "X-Kizuna-Key"
    test_settings.kizuna_callback_auth_value = SecretStr("secreto-kizuna")
    requests: list[httpx.Request] = []
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([200], seen=requests)).dispatch([notification_id])

    (request,) = requests
    assert (request.method, str(request.url)) == ("POST", CALLBACK_URL)
    assert request.headers["X-Kizuna-Key"] == "secreto-kizuna"
    assert request.headers["Content-Type"] == "application/json"


async def test_no_auth_header_when_not_configured(
    session_factory: Sessions, test_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    requests: list[httpx.Request] = []
    notification_id = await create_notification(session_factory, test_settings)
    await make_notifier(sequence_transport([200], seen=requests)).dispatch([notification_id])
    assert set(requests[0].headers) == {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-length",
        "content-type",
    }


async def test_unknown_notification_is_ignored(
    make_notifier: Callable[..., KizunaNotifier], db_engine: Any
) -> None:
    await make_notifier(sequence_transport([200])).dispatch([uuid.uuid4()])


async def test_logs_never_contain_document_or_auth_value(
    api_client: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    use_transport: Callable,
    kizuna_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    kizuna_settings.kizuna_callback_auth_header = "X-Kizuna-Key"
    kizuna_settings.kizuna_callback_auth_value = SecretStr("secreto-kizuna")
    use_transport(sequence_transport([500, 422]))
    with caplog.at_level(logging.DEBUG):
        await answered(api_client, whatsapp_sender, ACCEPT)

    # Los loggers "httpx" y "kizuna_mock" no son de la aplicación (cliente de prueba y mock).
    logged = "\n".join(
        f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name not in ("httpx", "kizuna_mock")
    )
    assert "Respuesta" in logged or "KIZUNA" in logged
    for secret in ("12345678", "secreto-kizuna", "987654321", "JUAN"):
        assert secret not in logged


# --- KizunaClient ---


async def test_client_marks_success_and_retryable(test_settings: Settings) -> None:
    async def send(code: int) -> Any:
        async with KizunaClient.build_http_client(test_settings, sequence_transport([code])) as http_client:
            return await KizunaClient.from_settings(http_client, test_settings).send(CALLBACK_URL, {})

    assert (await send(200)).ok is True
    assert (await send(204)).ok is True
    assert ((await send(500)).ok, (await send(500)).retryable) == (False, True)
    assert (await send(429)).retryable is True
    assert ((await send(404)).ok, (await send(404)).retryable) == (False, False)
    # Una redirección no se sigue y no se reintenta.
    assert ((await send(302)).ok, (await send(302)).retryable) == (False, False)


# --- Configuración al arrancar ---


def test_startup_warning_when_callback_disabled(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://x", kizuna_callback_enabled=False)
    with caplog.at_level(logging.WARNING, logger="app.integrations.kizuna_client"):
        check_kizuna_settings(settings)
    assert any("KIZUNA_CALLBACK_ENABLED=false" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"kizuna_callback_url": ""}, "falta KIZUNA_CALLBACK_URL"),
        (
            {"kizuna_callback_auth_header": "X-Kizuna-Key", "kizuna_callback_auth_value": ""},
            "falta KIZUNA_CALLBACK_AUTH_VALUE",
        ),
    ],
)
def test_startup_fails_when_enabled_with_incomplete_settings(values: dict[str, str], problem: str) -> None:
    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://x", kizuna_callback_enabled=True, **values
    )
    with pytest.raises(KizunaConfigurationError, match=problem):
        check_kizuna_settings(settings)


def test_startup_ok_when_enabled_and_configured(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x",
        kizuna_callback_enabled=True,
        kizuna_callback_url=CALLBACK_URL,
        kizuna_callback_auth_header="X-Kizuna-Key",
        kizuna_callback_auth_value="secreto",
    )
    with caplog.at_level(logging.WARNING, logger="app.integrations.kizuna_client"):
        check_kizuna_settings(settings)
    assert caplog.records == []


# --- Reclamo (SKIP LOCKED) y reproceso de las PENDIENTE al arrancar ---


async def set_notification(sessions: Sessions, notification_id: uuid.UUID, **values: Any) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(KizunaNotification).where(KizunaNotification.id == notification_id).values(**values)
        )


async def get_notification(sessions: Sessions, notification_id: uuid.UUID) -> KizunaNotification:
    async with sessions() as session:
        return await session.get(KizunaNotification, notification_id)


async def create_many(sessions: Sessions, settings: Settings, count: int) -> list[uuid.UUID]:
    return [
        await (  # Un cliente distinto por notificación: solo puede haber un principal activo por documento + Dealer.
            create_notification(
                sessions, settings, transaction_id=f"TRX-{i}", document_number=f"{10000000 + i}"
            )
        )
        for i in range(count)
    ]


def minutes_ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


# --- Reclamo en el envío normal ---


async def test_dispatch_claims_before_sending(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    notification_id = await create_notification(session_factory, kizuna_settings)
    await make_notifier().dispatch([notification_id])

    notification = await get_notification(session_factory, notification_id)
    assert notification.status == NotificationStatus.ENVIADO
    assert notification.claimed_at == notification.last_attempt_at


async def test_disabled_dispatch_leaves_the_notification_unclaimed(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    kizuna_settings.kizuna_callback_enabled = False
    notification_id = await create_notification(session_factory, kizuna_settings)
    await make_notifier().dispatch([notification_id])
    notification = await get_notification(session_factory, notification_id)
    assert (notification.status, notification.claimed_at) == (NotificationStatus.PENDIENTE, None)


async def test_dispatch_skips_a_notification_claimed_by_another_instance(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    notification_id = await create_notification(session_factory, kizuna_settings)
    await set_notification(session_factory, notification_id, claimed_at=minutes_ago(1))
    await make_notifier().dispatch([notification_id])
    assert mock_state.total_requests == 0


async def test_claim_is_renewed_on_each_attempt(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    seen: list[bool] = []

    async def check_claim(seconds: float) -> None:
        notification = await get_notification(session_factory, notification_id)
        seen.append(notification.claimed_at == notification.last_attempt_at)

    notification_id = await create_notification(session_factory, kizuna_settings)
    notifier = make_notifier(sequence_transport([500, 500, 200]))
    notifier._sleep = check_claim
    await notifier.dispatch([notification_id])
    assert seen == [True, True]


async def test_claim_skips_locked_rows_without_waiting(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    locked_id, free_id = await create_many(session_factory, kizuna_settings, 2)
    async with session_factory.begin() as session:
        # Otra instancia tiene bloqueada una notificación mientras la reclama.
        await session.execute(
            select(KizunaNotification).where(KizunaNotification.id == locked_id).with_for_update()
        )
        claimed = await asyncio.wait_for(make_notifier().claim(), timeout=5)
    assert claimed == [free_id]


# --- Reproceso al arrancar ---


async def test_reprocess_sends_everything_accumulated_while_disabled(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    # Con el envío apagado se acumulan PENDIENTE sin reclamar: una de hace días y otras recién creadas.
    kizuna_settings.kizuna_callback_enabled = False
    ids = await create_many(session_factory, kizuna_settings, 3)
    await set_notification(session_factory, ids[0], created_at=minutes_ago(3 * 24 * 60))
    await make_notifier().dispatch(ids)
    assert mock_state.total_requests == 0

    # Se enciende el envío y la app arranca: se envían todas, antiguas y recientes.
    kizuna_settings.kizuna_callback_enabled = True
    assert await make_notifier().reprocess_pending() == 3
    assert {n.status for n in await all_rows(session_factory, KizunaNotification)} == {
        NotificationStatus.ENVIADO
    }
    assert sorted(item["payload"]["transaction_id"] for item in mock_state.received) == [
        "TRX-0",
        "TRX-1",
        "TRX-2",
    ]


async def test_reprocess_takes_expired_claims_and_skips_recent_ones(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    in_flight, abandoned = await create_many(session_factory, kizuna_settings, 2)
    # Otra instancia la está reintentando (último intento hace 1 minuto).
    await set_notification(
        session_factory, in_flight, claimed_at=minutes_ago(1), attempts=1, last_attempt_at=minutes_ago(1)
    )
    # La instancia que la enviaba se reinició: más de 10 minutos sin intento.
    await set_notification(
        session_factory, abandoned, claimed_at=minutes_ago(11), attempts=2, last_attempt_at=minutes_ago(11)
    )

    assert await make_notifier().reprocess_pending() == 1
    assert (await get_notification(session_factory, in_flight)).status == NotificationStatus.PENDIENTE
    retaken = await get_notification(session_factory, abandoned)
    # Sigue contando sus intentos anteriores.
    assert (retaken.status, retaken.attempts) == (NotificationStatus.ENVIADO, 3)
    assert [item["payload"]["transaction_id"] for item in mock_state.received] == ["TRX-1"]


async def test_reprocess_respects_reprocess_after_minutes(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    kizuna_settings.kizuna_callback_reprocess_after_minutes = 30
    notification_id = await create_notification(session_factory, kizuna_settings)
    await set_notification(session_factory, notification_id, claimed_at=minutes_ago(11))
    assert await make_notifier().reprocess_pending() == 0
    await set_notification(session_factory, notification_id, claimed_at=minutes_ago(31))
    assert await make_notifier().reprocess_pending() == 1


@pytest.mark.parametrize("final_status", [NotificationStatus.ENVIADO, NotificationStatus.FALLIDO])
async def test_reprocess_ignores_final_notifications(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
    final_status: NotificationStatus,
) -> None:
    notification_id = await create_notification(session_factory, kizuna_settings)
    await set_notification(session_factory, notification_id, status=final_status, claimed_at=minutes_ago(60))
    assert await make_notifier().reprocess_pending() == 0
    assert mock_state.total_requests == 0


async def test_reprocess_disabled_does_nothing(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    kizuna_settings.kizuna_callback_enabled = False
    notification_id = await create_notification(session_factory, kizuna_settings)
    assert await make_notifier().reprocess_pending() == 0
    assert (await get_notification(session_factory, notification_id)).claimed_at is None
    assert mock_state.total_requests == 0


async def test_two_instances_reprocessing_at_once_send_each_notification_once(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    await create_many(session_factory, kizuna_settings, 8)
    claimed = await asyncio.gather(*(make_notifier().reprocess_pending() for _ in range(3)))

    assert sum(claimed) == 8
    assert mock_state.total_requests == 8
    assert sorted(item["payload"]["transaction_id"] for item in mock_state.received) == [
        f"TRX-{i}" for i in range(8)
    ]


async def test_claim_returns_the_oldest_first(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    ids = await create_many(session_factory, kizuna_settings, 4)
    for position, notification_id in enumerate(reversed(ids)):
        await set_notification(session_factory, notification_id, created_at=minutes_ago(100 - position))
    assert await make_notifier().claim() == list(reversed(ids))


async def test_reprocess_limits_concurrent_sends(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return httpx.Response(200)

    await create_many(session_factory, kizuna_settings, 15)
    assert await make_notifier(httpx.MockTransport(handler)).reprocess_pending() == 15
    assert peak == REPROCESS_CONCURRENCY
    assert {n.status for n in await all_rows(session_factory, KizunaNotification)} == {
        NotificationStatus.ENVIADO
    }


# --- Arranque de la app ---


async def test_reprocess_runs_in_background_on_startup(
    session_factory: Sessions,
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    mock_state: kizuna_mock.MockState,
) -> None:
    await create_notification(session_factory, kizuna_settings)
    async with reprocess_pending_in_background(make_notifier()) as task:
        await asyncio.wait_for(task, timeout=5)
    assert mock_state.total_requests == 1


async def test_shutdown_cancels_the_reprocess_and_keeps_the_claim(
    session_factory: Sessions, kizuna_settings: Settings, make_notifier: Callable[..., KizunaNotifier]
) -> None:
    waiting = asyncio.Event()

    async def wait_forever(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    notification_id = await create_notification(session_factory, kizuna_settings)
    notifier = make_notifier(sequence_transport([500]))
    notifier._sleep = wait_forever
    async with reprocess_pending_in_background(notifier) as task:
        await asyncio.wait_for(waiting.wait(), timeout=5)
    assert task.cancelled()

    # Queda PENDIENTE con su intento y su reclamo: el próximo arranque la retoma cuando venza.
    notification = await get_notification(session_factory, notification_id)
    assert (notification.status, notification.attempts) == (NotificationStatus.PENDIENTE, 1)
    assert notification.claimed_at is not None


async def test_reprocess_error_does_not_stop_the_app(
    kizuna_settings: Settings,
    make_notifier: Callable[..., KizunaNotifier],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = make_notifier()

    async def broken_claim(*args: object) -> list[uuid.UUID]:
        raise ConnectionError("la base no responde")

    monkeypatch.setattr(notifier, "claim", broken_claim)
    with caplog.at_level(logging.ERROR, logger="app.services.kizuna_notifications"):
        async with reprocess_pending_in_background(notifier) as task:
            await asyncio.wait_for(task, timeout=5)
    assert [r.getMessage() for r in caplog.records] == ["Error en el reproceso de notificaciones a KIZUNA"]


async def test_app_lifespan_starts_and_stops() -> None:
    from app.main import app

    # Con el .env local (KIZUNA_CALLBACK_ENABLED=false) el reproceso no hace nada; solo se verifica la conexión.
    async with app.router.lifespan_context(app):
        pass


def test_startup_warning_when_reprocess_window_is_too_short(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x",
        kizuna_callback_enabled=True,
        kizuna_callback_backoff_seconds=[5, 30, 120],
        kizuna_callback_timeout_seconds=10,
        kizuna_callback_reprocess_after_minutes=2,
    )
    with caplog.at_level(logging.WARNING, logger="app.integrations.kizuna_client"):
        check_kizuna_settings(settings)
    assert any("podría enviarse dos veces" in r.getMessage() for r in caplog.records)
