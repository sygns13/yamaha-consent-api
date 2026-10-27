"""scripts/issue_local_link.py: token del enlace para probar en local (Postman, landing)."""

import re
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from scripts import issue_local_link
from tests.conftest import TEST_API_KEY, RecordingWhatsAppSender
from tests.db_helpers import Sessions

BODY: dict[str, Any] = {
    "transaction_id": "TRX-1",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan pérez",
    "phone": "987654321",
    "dealer": "DEALER-001",
}


@pytest.fixture(autouse=True)
def use_test_database(session_factory: Sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issue_local_link, "SessionLocal", session_factory)


async def register(client: httpx.AsyncClient, **overrides: Any) -> None:
    response = await client.post(
        "/api/v1/consents", json={**BODY, **overrides}, headers={"X-API-Key": TEST_API_KEY}
    )
    assert response.status_code in (200, 201), response.text


async def link_status(client: httpx.AsyncClient, token: str) -> int | str:
    response = await client.get(f"/api/v1/public/consent-links/{token}")
    return response.json()["link_status"] if response.status_code == 200 else response.status_code


async def test_issued_token_opens_the_link_and_replaces_the_old_one(
    api_client: httpx.AsyncClient, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await register(api_client)
    whatsapp_token = whatsapp_sender.calls[-1]["token"]

    token = await issue_local_link.issue_link("TRX-1")

    assert token != whatsapp_token
    assert await link_status(api_client, token) == "VALID"
    # Un solo enlace por registro, como en Producción: el token del WhatsApp deja de servir.
    assert await link_status(api_client, whatsapp_token) == 404
    submitted = await api_client.post(
        f"/api/v1/public/consent-links/{token}/submit",
        json={"personal_data": True, "marketing_yamaha": True, "marketing_dealer": True},
    )
    assert submitted.json() == {"status": "ACEPTADO"}


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("missing", "No existe la transacción"),
        ("copy", "es una copia vinculada"),
        ("answered", "ya tiene respuesta (ACEPTADO)"),
    ],
)
async def test_refuses_transactions_without_a_usable_link(
    api_client: httpx.AsyncClient, whatsapp_sender: RecordingWhatsAppSender, setup: str, message: str
) -> None:
    target = "TRX-1"
    if setup != "missing":
        await register(api_client)
    if setup == "copy":
        await register(api_client, transaction_id="TRX-2")
        target = "TRX-2"
    if setup == "answered":
        token = whatsapp_sender.calls[-1]["token"]
        await api_client.post(
            f"/api/v1/public/consent-links/{token}/submit",
            json={"personal_data": True, "marketing_yamaha": False, "marketing_dealer": False},
        )

    with pytest.raises(issue_local_link.IssueLinkError, match=re.escape(message)):
        await issue_local_link.issue_link(target)


async def test_refuses_to_run_in_production(
    monkeypatch: pytest.MonkeyPatch, test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    test_settings.app_env = "production"
    monkeypatch.setattr(issue_local_link, "get_settings", lambda: test_settings)
    assert await issue_local_link.main("TRX-1") == 2
    assert "solo para desarrollo local" in capsys.readouterr().err
