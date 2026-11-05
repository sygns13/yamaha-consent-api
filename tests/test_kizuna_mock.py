import logging
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from mocks.kizuna_mock.main import app

# Contrato de KIZUNA.
PAYLOAD = {
    "IdentifierId": "100002001",
    "customerId": "12345678",
    "dealerCode": "CL0001",
    "consentDateTime": "2026-11-02T13:35:00-05:00",
    "personalDataConsent": True,
    "marketingConsentYMDP": False,
    "marketingConsentDEALER": False,
}
URL = "/kizuna/consent-result"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.delenv("MOCK_FAIL_TIMES", raising=False)
    monkeypatch.delenv("MOCK_RESPONSE_DELAY_SECONDS", raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        await c.delete("/kizuna/received")
        yield c


async def test_accepts_valid_payload(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json=PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {"received": True}

    listing = (await client.get("/kizuna/received")).json()
    assert listing["total_requests"] == 1
    assert listing["received"][0]["payload"]["IdentifierId"] == "100002001"
    assert listing["received"][0]["payload"]["consentDateTime"] == "2026-11-02T13:35:00-05:00"


async def test_accepts_rejection_with_all_false(client: httpx.AsyncClient) -> None:
    payload = {**PAYLOAD, "personalDataConsent": False}
    assert (await client.post(URL, json=payload)).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {k: v for k, v in PAYLOAD.items() if k != "IdentifierId"},
        {**PAYLOAD, "marketingConsentYMDP": None},
        {**PAYLOAD, "consentDateTime": "ayer"},
    ],
)
async def test_rejects_invalid_structure(client: httpx.AsyncClient, payload: dict) -> None:
    assert (await client.post(URL, json=payload)).status_code == 422


async def test_fail_times_returns_500_first_n_requests(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_FAIL_TIMES", "3")
    codes = [(await client.post(URL, json=PAYLOAD)).status_code for _ in range(4)]
    assert codes == [500, 500, 500, 200]

    listing = (await client.get("/kizuna/received")).json()
    assert listing["total_requests"] == 4
    assert len(listing["received"]) == 1


async def test_response_delay(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_RESPONSE_DELAY_SECONDS", "0.2")
    start = time.perf_counter()
    await client.post(URL, json=PAYLOAD)
    assert time.perf_counter() - start >= 0.2


async def test_logs_masked_document(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="kizuna_mock"):
        await client.post(URL, json=PAYLOAD)
    assert "****5678" in caplog.text
    assert "12345678" not in caplog.text
