"""Envío por Twilio (M3) con la API de Twilio simulada.

Se usa el Client real del SDK con un HttpClient falso: así se prueba lo que el SDK enviaría (campos, URL) y cómo
interpreta las respuestas de error de Twilio, sin salir a la red.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests
from twilio.http import HttpClient
from twilio.http.response import Response
from twilio.rest import Client

from app.core.config import Settings
from app.integrations.twilio_client import (
    TwilioConfigurationError,
    TwilioWhatsAppSender,
    check_twilio_settings,
)
from app.integrations.whatsapp import FakeWhatsAppSender, get_whatsapp_sender
from app.models import Consent, WhatsappMessage, WhatsappStatus
from tests.db_helpers import Sessions, all_rows, get_consent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_SID = "ACtest0000000000000000000000000000"
FROM = "whatsapp:+14155238886"
CONTENT_SID = "HXtest00000000000000000000000000000"
CALLBACK = "https://api.ejemplo.com/api/v1/webhooks/twilio/status"
TO = "whatsapp:+51987654321"
TOKEN = "token-secreto-del-enlace"
MESSAGES_URI = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"


class FakeTwilioHttp(HttpClient):
    """Responde como la API de Twilio. `response` es (status, json) o una excepción a lanzar."""

    def __init__(self, response: tuple[int, dict[str, Any]] | Exception | None = None) -> None:
        super().__init__(logger=logging.getLogger("tests.fake_twilio"), is_async=False)
        self.requests: list[dict[str, Any]] = []
        self.response = response or (201, {"sid": "SM" + "a" * 32, "status": "queued", "error_code": None})

    def request(
        self,
        method,
        uri,
        params=None,
        data=None,
        headers=None,
        auth=None,
        timeout=None,
        allow_redirects=False,
    ):
        self.requests.append({"method": method, "uri": uri, "data": dict(data or {}), "auth": auth})
        if isinstance(self.response, Exception):
            raise self.response
        status, body = self.response
        return Response(status, json.dumps(body))


def make_sender(http: FakeTwilioHttp, *, status_callback: str | None = CALLBACK) -> TwilioWhatsAppSender:
    client = Client(ACCOUNT_SID, "auth-token", http_client=http)
    return TwilioWhatsAppSender(client, from_=FROM, content_sid=CONTENT_SID, status_callback=status_callback)


async def send(sender: TwilioWhatsAppSender):
    return await sender.send_consent_link(to=TO, token=TOKEN, transaction_id="TRX-1")


def twilio_error(status: int, code: int, message: str) -> tuple[int, dict[str, Any]]:
    return status, {
        "code": code,
        "message": message,
        "more_info": f"https://www.twilio.com/docs/errors/{code}",
        "status": status,
    }


# --- Petición a Twilio ---


async def test_sends_call_to_action_template_with_token_variable() -> None:
    http = FakeTwilioHttp()
    await send(make_sender(http))

    (request,) = http.requests
    assert request["method"] == "POST"
    assert request["uri"] == MESSAGES_URI
    assert request["auth"] == (ACCOUNT_SID, "auth-token")
    assert request["data"] == {
        "To": TO,
        "From": FROM,
        "ContentSid": CONTENT_SID,
        "ContentVariables": json.dumps({"1": TOKEN}),
        "StatusCallback": CALLBACK,
    }
    # Solo la plantilla: sin cuerpo de texto libre.
    assert "Body" not in request["data"]


async def test_status_callback_is_omitted_when_not_configured() -> None:
    http = FakeTwilioHttp()
    await send(make_sender(http, status_callback=""))
    assert "StatusCallback" not in http.requests[0]["data"]


# --- Respuestas de Twilio ---


async def test_success_returns_sid_and_status() -> None:
    result = await send(
        make_sender(FakeTwilioHttp((201, {"sid": "SM123", "status": "queued", "error_code": None})))
    )
    assert (result.status, result.message_sid, result.error_code, result.error_message) == (
        WhatsappStatus.QUEUED,
        "SM123",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("twilio_status", "expected"),
    [
        ("queued", WhatsappStatus.QUEUED),
        ("sent", WhatsappStatus.SENT),
        ("accepted", WhatsappStatus.QUEUED),
        ("sending", WhatsappStatus.QUEUED),
        ("scheduled", WhatsappStatus.QUEUED),
        ("algo-nuevo", WhatsappStatus.QUEUED),
        (None, WhatsappStatus.QUEUED),
    ],
)
async def test_creation_status_is_mapped_to_whatsapp_status(
    twilio_status: str | None, expected: WhatsappStatus
) -> None:
    result = await send(make_sender(FakeTwilioHttp((201, {"sid": "SM1", "status": twilio_status}))))
    assert result.status == expected


async def test_failed_status_on_creation_keeps_twilio_error() -> None:
    body = {"sid": "SM1", "status": "failed", "error_code": 63016, "error_message": "Fuera de la ventana"}
    result = await send(make_sender(FakeTwilioHttp((201, body))))
    assert (result.status, result.message_sid, result.error_code, result.error_message) == (
        WhatsappStatus.FAILED,
        "SM1",
        "63016",
        "Fuera de la ventana",
    )


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (400, 21211, "The 'To' number whatsapp:+51987654321 is not a valid phone number."),
        (400, 63016, "Failed to send freeform message because you are outside the allowed window."),
        (401, 20003, "Authenticate"),
        (404, 20404, "The requested resource was not found"),
        (429, 20429, "Too Many Requests"),
        (500, 20500, "Internal Server Error"),
    ],
)
async def test_twilio_error_is_returned_as_failed_not_raised(status: int, code: int, message: str) -> None:
    result = await send(make_sender(FakeTwilioHttp(twilio_error(status, code, message))))
    assert result.status == WhatsappStatus.FAILED
    assert result.message_sid is None
    assert result.error_code == str(code)
    assert result.error_message == message


@pytest.mark.parametrize(
    "error",
    [requests.ConnectionError("sin red"), requests.Timeout("timeout"), requests.exceptions.SSLError("ssl")],
)
async def test_network_errors_are_returned_as_failed_without_code(error: Exception) -> None:
    result = await send(make_sender(FakeTwilioHttp(error)))
    assert result.status == WhatsappStatus.FAILED
    assert (result.message_sid, result.error_code) == (None, None)
    assert result.error_message.startswith(type(error).__name__)


async def test_long_error_messages_are_truncated() -> None:
    result = await send(make_sender(FakeTwilioHttp(twilio_error(400, 21211, "x" * 5000))))
    assert len(result.error_message) == 1000


async def test_logs_mask_the_phone_and_never_contain_the_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await send(make_sender(FakeTwilioHttp()))
        await send(make_sender(FakeTwilioHttp(twilio_error(400, 21211, "The 'To' number is not valid"))))
        await send(make_sender(FakeTwilioHttp(requests.ConnectionError("sin red"))))

    assert TOKEN not in caplog.text
    assert "987654321" not in caplog.text
    sent = next(r for r in caplog.records if r.getMessage() == "WhatsApp enviado a Twilio")
    assert sent.to.endswith("4321")
    rejected = next(r for r in caplog.records if r.getMessage() == "Twilio rechazó el envío del WhatsApp")
    assert rejected.error_code == 21211


# --- Construcción desde la configuración ---


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "twilio_send_enabled": True,
        "twilio_account_sid": ACCOUNT_SID,
        "twilio_auth_token": "auth-token",
        "twilio_whatsapp_from": FROM,
        "twilio_content_sid": CONTENT_SID,
        "twilio_status_callback_url": CALLBACK,
        "twilio_timeout_seconds": 7,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_from_settings_uses_credentials_and_timeout() -> None:
    sender = TwilioWhatsAppSender.from_settings(make_settings())
    assert sender._client.username == ACCOUNT_SID
    assert sender._client.password == "auth-token"
    assert sender._client.http_client.timeout == 7


def test_send_disabled_uses_the_fake_sender() -> None:
    assert isinstance(get_whatsapp_sender(make_settings(twilio_send_enabled=False)), FakeWhatsAppSender)


def test_send_enabled_uses_twilio_and_reuses_the_client() -> None:
    settings = make_settings()
    sender = get_whatsapp_sender(settings)
    assert isinstance(sender, TwilioWhatsAppSender)
    assert get_whatsapp_sender(settings) is sender
    assert get_whatsapp_sender(make_settings(twilio_content_sid="HXotro")) is not sender


# --- Chequeo al arrancar ---


def test_startup_warning_when_send_disabled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.twilio_client"):
        check_twilio_settings(make_settings(twilio_send_enabled=False, twilio_account_sid=""))
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert "TWILIO_SEND_ENABLED=false" in record.getMessage()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"twilio_account_sid": ""}, "TWILIO_ACCOUNT_SID"),
        ({"twilio_auth_token": " "}, "TWILIO_AUTH_TOKEN"),
        ({"twilio_whatsapp_from": ""}, "TWILIO_WHATSAPP_FROM"),
        ({"twilio_content_sid": ""}, "TWILIO_CONTENT_SID"),
        ({"twilio_account_sid": "", "twilio_content_sid": ""}, "TWILIO_ACCOUNT_SID, TWILIO_CONTENT_SID"),
        ({"twilio_whatsapp_from": "+14155238886"}, "formato whatsapp:+"),
    ],
)
def test_startup_fails_when_send_enabled_with_incomplete_config(
    overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(TwilioConfigurationError, match="configuración de Twilio está incompleta") as exc:
        check_twilio_settings(make_settings(**overrides))
    assert expected in str(exc.value)


def test_startup_warning_when_status_callback_is_missing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.twilio_client"):
        check_twilio_settings(make_settings(twilio_status_callback_url=""))
    (record,) = caplog.records
    assert "TWILIO_STATUS_CALLBACK_URL" in record.getMessage()


def test_no_startup_log_when_fully_configured(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.integrations.twilio_client"):
        check_twilio_settings(make_settings())
    assert caplog.records == []


def test_app_does_not_start_when_send_enabled_without_config() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        check=False,
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
            "API_KEY_ENABLED": "false",
            "TWILIO_SEND_ENABLED": "true",
            "TWILIO_ACCOUNT_SID": "",
            "TWILIO_AUTH_TOKEN": "",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode != 0
    assert "TwilioConfigurationError" in result.stderr


# --- Integración con POST /api/v1/consents ---

BODY = {
    "transaction_id": "TRX-T1",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan pérez",
    "phone": "+51 987 654 321",
    "dealer": "DEALER-001",
}


@pytest.fixture
def twilio_http() -> FakeTwilioHttp:
    return FakeTwilioHttp()


@pytest.fixture
def twilio_api_client(api_client: httpx.AsyncClient, twilio_http: FakeTwilioHttp) -> httpx.AsyncClient:
    """api_client con el sender de Twilio real (sobre la API simulada) en lugar del sender de prueba."""
    from app.main import app

    app.dependency_overrides[get_whatsapp_sender] = lambda: make_sender(twilio_http)
    return api_client


async def test_post_sends_through_twilio_and_stores_the_message(
    twilio_api_client: httpx.AsyncClient, twilio_http: FakeTwilioHttp, session_factory: Sessions
) -> None:
    twilio_http.response = (201, {"sid": "SMabc123", "status": "queued"})
    response = await twilio_api_client.post("/api/v1/consents", json=BODY)

    assert response.status_code == 201
    assert response.json()["whatsapp"] == {"status": "queued", "error_code": None}
    (request,) = twilio_http.requests
    assert request["data"]["To"] == "whatsapp:+51987654321"
    token = json.loads(request["data"]["ContentVariables"])["1"]
    assert len(token) >= 43

    (message,) = await all_rows(session_factory, WhatsappMessage)
    assert (message.twilio_message_sid, message.status, message.to_phone) == (
        "SMabc123",
        WhatsappStatus.QUEUED,
        "whatsapp:+51987654321",
    )


async def test_post_keeps_the_record_when_twilio_rejects(
    twilio_api_client: httpx.AsyncClient, twilio_http: FakeTwilioHttp, session_factory: Sessions
) -> None:
    twilio_http.response = twilio_error(400, 21211, "The 'To' number is not a valid phone number.")
    response = await twilio_api_client.post("/api/v1/consents", json=BODY)

    assert response.status_code == 201
    assert response.json()["whatsapp"] == {"status": "failed", "error_code": "21211"}
    consent = await get_consent(session_factory, "TRX-T1")
    assert isinstance(consent, Consent)
    (message,) = await all_rows(session_factory, WhatsappMessage)
    assert (message.twilio_message_sid, message.status, message.error_code, message.error_message) == (
        None,
        WhatsappStatus.FAILED,
        "21211",
        "The 'To' number is not a valid phone number.",
    )


async def test_post_keeps_the_record_when_twilio_is_unreachable(
    twilio_api_client: httpx.AsyncClient, twilio_http: FakeTwilioHttp, session_factory: Sessions
) -> None:
    twilio_http.response = requests.ConnectionError("sin red")
    response = await twilio_api_client.post("/api/v1/consents", json=BODY)

    assert response.status_code == 201
    assert response.json()["whatsapp"] == {"status": "failed", "error_code": None}
    (message,) = await all_rows(session_factory, WhatsappMessage)
    assert message.error_message.startswith("ConnectionError")
