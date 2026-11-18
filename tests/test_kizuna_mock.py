import base64
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
    for name in (
        "MOCK_FAIL_TIMES",
        "MOCK_RESPONSE_DELAY_SECONDS",
        "MOCK_REQUIRE_AUTH",
        "MOCK_CLIENT_ID",
        "MOCK_CLIENT_SECRET",
        "MOCK_TOKEN_EXPIRES_IN",
        "MOCK_TOKEN_FAIL_TIMES",
    ):
        monkeypatch.delenv(name, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        await c.delete("/kizuna/received")
        await c.delete("/kizuna/oauth/tokens")
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


# --- OAuth 2.0 (INTEGRACION_KIZUNA_OAUTH2.spec.md, sección 6) ---

TOKEN_URL = "/kizuna/oauth/token"
TOKEN_FORM = {
    "grant_type": "client_credentials",
    "client_id": "mock-client",
    "client_secret": "mock-secret",
    "scope": "api://kizuna-mock/.default",
}


async def get_token(client: httpx.AsyncClient) -> str:
    response = await client.post(TOKEN_URL, data=TOKEN_FORM)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_token_with_credentials_in_the_body(client: httpx.AsyncClient) -> None:
    response = await client.post(TOKEN_URL, data=TOKEN_FORM)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token_type", "expires_in", "ext_expires_in", "access_token"}
    assert (body["token_type"], body["expires_in"], body["ext_expires_in"]) == ("Bearer", 3600, 3600)
    assert (await client.get("/kizuna/received")).json()["token_requests"] == 1


async def test_token_with_basic_credentials(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_CLIENT_SECRET", "a:b c+d")
    basic = base64.b64encode(b"mock-client:a%3Ab+c%2Bd").decode()
    response = await client.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials", "scope": "x/.default"},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("data", "headers", "status_code", "error"),
    [
        ({**TOKEN_FORM, "client_secret": "otro"}, {}, 401, "invalid_client"),
        ({k: v for k, v in TOKEN_FORM.items() if k != "client_secret"}, {}, 401, "invalid_client"),
        ({k: v for k, v in TOKEN_FORM.items() if k != "grant_type"}, {}, 400, "invalid_request"),
        ({**TOKEN_FORM, "grant_type": "password"}, {}, 400, "unsupported_grant_type"),
        ({k: v for k, v in TOKEN_FORM.items() if k != "scope"}, {}, 400, "invalid_request"),
        (TOKEN_FORM, {"Authorization": "Basic bW9jay1jbGllbnQ6bW9jay1zZWNyZXQ="}, 400, "invalid_request"),
    ],
)
async def test_token_errors_follow_rfc_6749(
    client: httpx.AsyncClient, data: dict, headers: dict, status_code: int, error: str
) -> None:
    response = await client.post(TOKEN_URL, data=data, headers=headers)
    assert (response.status_code, response.json()["error"]) == (status_code, error)
    assert response.json()["error_description"]


async def test_token_requires_form_urlencoded(client: httpx.AsyncClient) -> None:
    response = await client.post(TOKEN_URL, json=TOKEN_FORM)
    assert (response.status_code, response.json()["error"]) == (400, "invalid_request")


async def test_token_fail_times_and_expires_in(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_TOKEN_FAIL_TIMES", "1")
    monkeypatch.setenv("MOCK_TOKEN_EXPIRES_IN", "60")
    assert (await client.post(TOKEN_URL, data=TOKEN_FORM)).status_code == 500
    response = await client.post(TOKEN_URL, data=TOKEN_FORM)
    assert (response.status_code, response.json()["expires_in"]) == (200, 60)


async def test_require_auth_accepts_only_issued_bearers(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_REQUIRE_AUTH", "true")
    token = await get_token(client)

    assert (await client.post(URL, json=PAYLOAD)).status_code == 401
    assert (await client.post(URL, json=PAYLOAD, headers={"Authorization": "Bearer otro"})).status_code == 401
    assert (
        await client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200

    # Revocación: el mismo token deja de servir.
    assert (await client.delete("/kizuna/oauth/tokens")).status_code == 204
    assert (
        await client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401

    listing = (await client.get("/kizuna/received")).json()
    assert listing["total_requests"] == 4
    assert [item["authenticated"] for item in listing["received"]] == [True]


async def test_without_require_auth_records_whether_the_bearer_was_valid(client: httpx.AsyncClient) -> None:
    token = await get_token(client)
    await client.post(URL, json=PAYLOAD)
    await client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})

    listing = (await client.get("/kizuna/received")).json()
    assert [item["authenticated"] for item in listing["received"]] == [False, True]


async def test_reset_clears_token_requests_but_keeps_tokens(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_REQUIRE_AUTH", "true")
    token = await get_token(client)
    await client.delete("/kizuna/received")

    assert (await client.get("/kizuna/received")).json()["token_requests"] == 0
    assert (
        await client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200


async def test_logs_never_contain_secret_or_token(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_REQUIRE_AUTH", "true")
    with caplog.at_level(logging.DEBUG, logger="kizuna_mock"):
        token = await get_token(client)
        await client.post(TOKEN_URL, data={**TOKEN_FORM, "client_secret": "otro-secreto"})
        await client.post(URL, json=PAYLOAD, headers={"Authorization": f"Bearer {token}"})
        await client.post(URL, json=PAYLOAD, headers={"Authorization": "Bearer token-falso"})
    for secret in ("mock-secret", "otro-secreto", token, "token-falso"):
        assert secret not in caplog.text
