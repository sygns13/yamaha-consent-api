"""POST /api/v1/webhooks/twilio/status (M3): firma, eventos, estado del mensaje e idempotencia.

Los callbacks se firman con el RequestValidator real de Twilio, como lo hace Twilio.
"""

import asyncio
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.core.config import Settings
from app.integrations.twilio_client import check_twilio_settings
from app.integrations.whatsapp import WhatsAppSendResult
from app.models import WhatsappMessage, WhatsappMessageEvent, WhatsappStatus
from app.services.whatsapp_status import STATUS_RANK
from tests.conftest import RecordingWhatsAppSender
from tests.db_helpers import Sessions, all_rows

URL = "/api/v1/webhooks/twilio/status"
CALLBACK_URL = "https://api.ejemplo.com/api/v1/webhooks/twilio/status"
AUTH_TOKEN = "twilio-auth-token-de-prueba"
SID = "SM" + "1" * 32
PHONE = "whatsapp:+51987654321"


@pytest.fixture
def webhook_settings(test_settings: Settings) -> Settings:
    test_settings.twilio_auth_token = SecretStr(AUTH_TOKEN)
    test_settings.twilio_status_callback_url = CALLBACK_URL
    test_settings.twilio_signature_validation_enabled = True
    return test_settings


@pytest.fixture
async def sent_message(
    api_client: httpx.AsyncClient, whatsapp_sender: RecordingWhatsAppSender, webhook_settings: Settings
) -> str:
    """Un registro creado por POST /consents cuyo WhatsApp Twilio aceptó con SID (estado queued)."""
    whatsapp_sender.result = WhatsAppSendResult(status=WhatsappStatus.QUEUED, message_sid=SID)
    response = await api_client.post(
        "/api/v1/consents",
        json={
            "transaction_id": "TRX-W1",
            "document_type": "DNI",
            "document_number": "12345678",
            "full_name": "juan pérez",
            "phone": "987654321",
            "dealer": "DEALER-001",
        },
    )
    assert response.status_code == 201
    return SID


def payload(status: str = "sent", sid: str = SID, **extra: str) -> dict[str, str]:
    """Campos como los envía Twilio (incluye el teléfono en To)."""
    return {
        "MessageSid": sid,
        "SmsSid": sid,
        "MessageStatus": status,
        "SmsStatus": status,
        "AccountSid": "AC" + "0" * 32,
        "From": "whatsapp:+14155238886",
        "To": PHONE,
        "ApiVersion": "2010-04-01",
        **extra,
    }


def sign(params: dict[str, str], *, url: str = CALLBACK_URL, token: str = AUTH_TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, params)


async def callback(
    client: httpx.AsyncClient, params: dict[str, str], *, signature: str | None = None, **kwargs: Any
) -> httpx.Response:
    headers = {"X-Twilio-Signature": sign(params) if signature is None else signature, "X-API-Key": ""}
    return await client.post(URL, data=params, headers=headers, **kwargs)


async def message_row(sessions: Sessions) -> WhatsappMessage:
    async with sessions() as session:
        return await session.scalar(select(WhatsappMessage).where(WhatsappMessage.twilio_message_sid == SID))


async def events(sessions: Sessions) -> list[WhatsappMessageEvent]:
    return await all_rows(sessions, WhatsappMessageEvent)


# --- Firma válida ---


async def test_valid_signature_records_event_and_updates_message(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    params = payload("sent")
    response = await callback(api_client, params)

    assert response.status_code == 204
    assert response.content == b""
    (event,) = await events(session_factory)
    assert event.status == WhatsappStatus.SENT
    assert event.error_code is None
    assert event.raw_payload == params
    message = await message_row(session_factory)
    assert event.message_id == message.id
    assert message.status == WhatsappStatus.SENT


async def test_webhook_does_not_require_api_key(api_client: httpx.AsyncClient, sent_message: str) -> None:
    # callback() envía X-API-Key vacío: lo protege la firma, no la API Key.
    assert (await callback(api_client, payload("sent"))).status_code == 204


async def test_signature_is_checked_against_request_url_when_callback_url_is_empty(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str, webhook_settings: Settings
) -> None:
    webhook_settings.twilio_status_callback_url = ""
    params = payload("sent")
    response = await callback(api_client, params, signature=sign(params, url=f"http://test{URL}"))
    assert response.status_code == 204
    assert len(await events(session_factory)) == 1


# --- Firma inválida ---


@pytest.mark.parametrize(
    ("signature_for", "detail"),
    [
        ("otra-url", "Firma de Twilio inválida"),
        ("otro-token", "Firma de Twilio inválida"),
        ("basura", "Firma de Twilio inválida"),
        ("vacia", "Falta la firma de Twilio"),
    ],
)
async def test_invalid_signature_returns_403_and_changes_nothing(
    api_client: httpx.AsyncClient,
    session_factory: Sessions,
    sent_message: str,
    signature_for: str,
    detail: str,
) -> None:
    params = payload("delivered")
    signature = {
        "otra-url": sign(params, url="https://otro.com/webhook"),
        "otro-token": sign(params, token="otro-token"),
        "basura": "no-es-una-firma",
        "vacia": "",
    }[signature_for]

    response = await callback(api_client, params, signature=signature)
    assert response.status_code == 403
    assert response.json() == {"detail": detail}
    assert await events(session_factory) == []
    assert (await message_row(session_factory)).status == WhatsappStatus.QUEUED


async def test_missing_signature_header_returns_403(api_client: httpx.AsyncClient, sent_message: str) -> None:
    response = await api_client.post(URL, data=payload("sent"), headers={"X-API-Key": ""})
    assert response.status_code == 403
    assert response.json() == {"detail": "Falta la firma de Twilio"}


async def test_tampered_payload_returns_403(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    signed = payload("sent")
    tampered = {**signed, "MessageStatus": "read"}
    response = await callback(api_client, tampered, signature=sign(signed))
    assert response.status_code == 403
    assert await events(session_factory) == []


async def test_without_auth_token_every_callback_is_rejected(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str, webhook_settings: Settings
) -> None:
    webhook_settings.twilio_auth_token = SecretStr("")
    params = payload("sent")
    response = await callback(api_client, params, signature=sign(params, token=""))
    assert response.status_code == 403
    assert await events(session_factory) == []


async def test_validation_disabled_accepts_callbacks_without_signature(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str, webhook_settings: Settings
) -> None:
    webhook_settings.twilio_signature_validation_enabled = False
    assert (await api_client.post(URL, data=payload("sent"), headers={"X-API-Key": ""})).status_code == 204
    assert (await callback(api_client, payload("delivered"), signature="basura")).status_code == 204
    assert (await message_row(session_factory)).status == WhatsappStatus.DELIVERED


# --- Callbacks repetidos (idempotencia) ---


async def test_repeated_callback_is_recorded_once(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    params = payload("delivered")
    for _ in range(3):
        assert (await callback(api_client, params)).status_code == 204

    (event,) = await events(session_factory)
    assert event.status == WhatsappStatus.DELIVERED
    assert (await message_row(session_factory)).status == WhatsappStatus.DELIVERED


async def test_concurrent_repeated_callbacks_are_recorded_once(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    params = payload("delivered")
    responses = await asyncio.gather(*(callback(api_client, params) for _ in range(6)))
    assert {r.status_code for r in responses} == {204}
    assert len(await events(session_factory)) == 1


async def test_repeated_callback_does_not_undo_a_later_status(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    await callback(api_client, payload("sent"))
    await callback(api_client, payload("read"))
    await callback(api_client, payload("sent"))  # repetido y atrasado
    assert [e.status for e in await events(session_factory)] == [WhatsappStatus.SENT, WhatsappStatus.READ]
    assert (await message_row(session_factory)).status == WhatsappStatus.READ


# --- Avance del estado ---


async def test_status_advances_queued_sent_delivered_read(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    for status in ("sent", "delivered", "read"):
        assert (await callback(api_client, payload(status))).status_code == 204
        assert (await message_row(session_factory)).status == WhatsappStatus(status)
    assert [e.status.value for e in await events(session_factory)] == ["sent", "delivered", "read"]


async def test_out_of_order_callbacks_never_move_the_status_back(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    for status in ("read", "delivered", "sent"):
        await callback(api_client, payload(status))
    # Todos quedan como eventos, pero el mensaje sigue en el estado más avanzado.
    assert {e.status.value for e in await events(session_factory)} == {"read", "delivered", "sent"}
    assert (await message_row(session_factory)).status == WhatsappStatus.READ


async def test_failed_callback_stores_the_error(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    await callback(api_client, payload("sent"))
    await callback(
        api_client,
        payload("failed", ErrorCode="63016", ErrorMessage="Fuera de la ventana de 24 horas"),
    )

    message = await message_row(session_factory)
    assert (message.status, message.error_code, message.error_message) == (
        WhatsappStatus.FAILED,
        "63016",
        "Fuera de la ventana de 24 horas",
    )
    failed_event = next(e for e in await events(session_factory) if e.status == WhatsappStatus.FAILED)
    assert failed_event.error_code == "63016"


async def test_undelivered_after_sent_keeps_the_error_code(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    await callback(api_client, payload("sent"))
    await callback(api_client, payload("undelivered", ErrorCode="30008"))
    message = await message_row(session_factory)
    assert (message.status, message.error_code) == (WhatsappStatus.UNDELIVERED, "30008")


async def test_late_delivered_does_not_override_a_failure(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    await callback(api_client, payload("failed", ErrorCode="63016"))
    await callback(api_client, payload("delivered"))
    message = await message_row(session_factory)
    assert (message.status, message.error_code) == (WhatsappStatus.FAILED, "63016")


async def test_empty_error_code_is_stored_as_null(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    await callback(api_client, payload("delivered", ErrorCode=""))
    (event,) = await events(session_factory)
    assert event.error_code is None


async def test_in_progress_status_is_recorded_as_queued(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    assert (await callback(api_client, payload("sending"))).status_code == 204
    (event,) = await events(session_factory)
    assert event.status == WhatsappStatus.QUEUED
    assert event.raw_payload["MessageStatus"] == "sending"
    assert (await message_row(session_factory)).status == WhatsappStatus.QUEUED


# --- Casos que se ignoran (204) y validación ---


async def test_unknown_message_sid_is_ignored(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    response = await callback(api_client, payload("delivered", sid="SM" + "9" * 32))
    assert response.status_code == 204
    assert await events(session_factory) == []


async def test_unknown_status_is_ignored(
    api_client: httpx.AsyncClient, session_factory: Sessions, sent_message: str
) -> None:
    response = await callback(api_client, payload("canceled"))
    assert response.status_code == 204
    assert await events(session_factory) == []
    assert (await message_row(session_factory)).status == WhatsappStatus.QUEUED


async def test_missing_message_sid_returns_422(api_client: httpx.AsyncClient, sent_message: str) -> None:
    params = {k: v for k, v in payload("sent").items() if k != "MessageSid"}
    response = await callback(api_client, params)
    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "missing", "loc": ["body", "MessageSid"], "msg": "Campo obligatorio"}]
    }


async def test_logs_never_contain_the_phone_or_the_signature(
    api_client: httpx.AsyncClient, sent_message: str, caplog: pytest.LogCaptureFixture
) -> None:
    params = payload("delivered")
    signature = sign(params)
    with caplog.at_level(logging.DEBUG):
        await callback(api_client, params, signature=signature)
        await callback(api_client, params, signature="basura")
    assert "987654321" not in caplog.text
    assert signature not in caplog.text


def test_route_is_documented_without_api_key() -> None:
    from app.main import app

    operation = app.openapi()["paths"][URL]["post"]
    assert "security" not in operation
    assert {"204", "403"} <= set(operation["responses"])


def test_every_status_has_a_rank() -> None:
    assert set(STATUS_RANK) == set(WhatsappStatus)


# --- Avisos al arrancar ---


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "twilio_auth_token": AUTH_TOKEN,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_startup_warning_when_signature_validation_disabled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.twilio_client"):
        check_twilio_settings(_settings(twilio_signature_validation_enabled=False))
    messages = [r.getMessage() for r in caplog.records]
    assert any("TWILIO_SIGNATURE_VALIDATION_ENABLED=false" in m for m in messages)


def test_startup_warning_when_validation_enabled_without_auth_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.twilio_client"):
        check_twilio_settings(_settings(twilio_auth_token=""))
    messages = [r.getMessage() for r in caplog.records]
    assert any("rechazará todos los callbacks" in m for m in messages)
