"""Integración de punta a punta en local: KIZUNA → enlace → landing (GET y submit) → callback al mock de KIZUNA.

Usa el entorno local real:
- PostgreSQL de docker compose (base de pruebas `<db>_test`, ver conftest.py).
- El mock de KIZUNA de docker compose por HTTP real (`docker compose up -d kizuna-mock`, puerto 8081). Si no
  responde, los tests se saltan. Otra URL: KIZUNA_MOCK_URL=http://host:puerto.

La API corre en el proceso del test (ASGI): así se obtiene el token que llevaría el WhatsApp, que nunca se guarda ni
se loguea. El WhatsApp no se envía (sender de prueba). Las tareas en segundo plano (callback a KIZUNA) terminan antes
de que el cliente de prueba reciba la respuesta.

Ejecutar solo estos tests: pytest -m integration
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.models import ConsentLink, KizunaNotification, NotificationStatus, WhatsappMessage
from app.utils.tokens import hash_token
from tests.conftest import TEST_API_KEY, RecordingWhatsAppSender
from tests.db_helpers import Sessions, all_rows

pytestmark = pytest.mark.integration

MOCK_URL = os.getenv("KIZUNA_MOCK_URL", "http://localhost:8081").rstrip("/")
CUSTOMER: dict[str, Any] = {
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan  pérez   gómez",
    "phone": "+51 987 654 321",
    "dealer": "DEALER-001",
    "additional_info": {"canal": "tienda"},
}
ACCEPT = {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}
REJECT_ALL = {"personal_data": False, "marketing_yamaha": False, "marketing_dealer": False}


@pytest.fixture
async def kizuna_mock() -> AsyncIterator[httpx.AsyncClient]:
    """Cliente del mock de KIZUNA de docker compose, con la lista de recibidos vacía."""
    async with httpx.AsyncClient(base_url=MOCK_URL, timeout=5) as client:
        try:
            await client.delete("/kizuna/received")
        except httpx.HTTPError:
            pytest.skip(f"Mock de KIZUNA no disponible en {MOCK_URL}: docker compose up -d kizuna-mock")
        yield client


@pytest.fixture(autouse=True)
def e2e_settings(test_settings: Settings) -> Settings:
    test_settings.kizuna_callback_enabled = True
    test_settings.kizuna_callback_url = f"{MOCK_URL}/kizuna/consent-result"
    # Por si el contenedor tiene MOCK_FAIL_TIMES: esperas cortas entre reintentos.
    test_settings.kizuna_callback_backoff_seconds = [0.2]
    test_settings.privacy_policy_url = "https://www.yamaha.com.pe/politica-de-privacidad"
    return test_settings


@dataclass
class LocalFlow:
    """Los pasos del flujo, como los haría cada actor."""

    api: httpx.AsyncClient
    mock: httpx.AsyncClient
    sender: RecordingWhatsAppSender
    sessions: Sessions

    async def kizuna_sends(self, transaction_id: str, **overrides: Any) -> tuple[httpx.Response, str | None]:
        """KIZUNA → POST /api/v1/consents. Devuelve la respuesta y el token del WhatsApp (None si no se envió)."""
        sent_before = len(self.sender.calls)
        response = await self.api.post(
            "/api/v1/consents",
            json={**CUSTOMER, "transaction_id": transaction_id, **overrides},
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert response.status_code in (200, 201), response.text
        token = self.sender.calls[-1]["token"] if len(self.sender.calls) > sent_before else None
        return response, token

    async def customer_opens(self, token: str) -> dict[str, Any]:
        """Landing → GET /api/v1/public/consent-links/{token} (sin API Key)."""
        response = await self.api.get(f"/api/v1/public/consent-links/{token}", headers={"X-API-Key": ""})
        assert response.status_code == 200, response.text
        return response.json()

    async def customer_submits(self, token: str, answers: dict[str, bool]) -> httpx.Response:
        """Landing → POST /api/v1/public/consent-links/{token}/submit (sin API Key)."""
        return await self.api.post(
            f"/api/v1/public/consent-links/{token}/submit", json=answers, headers={"X-API-Key": ""}
        )

    async def kizuna_received(self) -> dict[str, dict[str, Any]]:
        """Payloads que recibió el mock de KIZUNA, por transaction_id."""
        received = (await self.mock.get("/kizuna/received")).json()["received"]
        payloads = [item["payload"] for item in received]
        by_id = {payload["transaction_id"]: payload for payload in payloads}
        assert len(by_id) == len(payloads), "KIZUNA recibió dos veces el mismo transaction_id"
        return by_id

    async def kizuna_queries(self, transaction_id: str) -> dict[str, Any]:
        response = await self.api.get(
            f"/api/v1/consents/{transaction_id}", headers={"X-API-Key": TEST_API_KEY}
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def notifications(self) -> dict[str, KizunaNotification]:
        return {n.transaction_id: n for n in await all_rows(self.sessions, KizunaNotification)}


@pytest.fixture
def flow(
    api_client: httpx.AsyncClient,
    kizuna_mock: httpx.AsyncClient,
    whatsapp_sender: RecordingWhatsAppSender,
    session_factory: Sessions,
) -> LocalFlow:
    return LocalFlow(api=api_client, mock=kizuna_mock, sender=whatsapp_sender, sessions=session_factory)


async def assert_link_generated(flow: LocalFlow, transaction_id: str, token: str, settings: Settings) -> None:
    """El enlace existe solo como hash, vence en LINK_TTL_HOURS y el WhatsApp salió al número normalizado."""
    links = [
        link for link in await all_rows(flow.sessions, ConsentLink) if link.transaction_id == transaction_id
    ]
    assert [link.token_hash for link in links] == [hash_token(token)]
    assert links[0].expires_at - links[0].created_at == timedelta(hours=settings.link_ttl_hours)
    messages = [
        m for m in await all_rows(flow.sessions, WhatsappMessage) if m.transaction_id == transaction_id
    ]
    assert [m.to_phone for m in messages] == ["whatsapp:+51987654321"]


# --- Caso aceptado ---


async def test_accepted(flow: LocalFlow, e2e_settings: Settings) -> None:
    response, token = await flow.kizuna_sends("TRX-OK-1")
    assert response.status_code == 201
    assert response.json()["result"] == "CREATED"
    assert token is not None
    await assert_link_generated(flow, "TRX-OK-1", token, e2e_settings)

    assert await flow.customer_opens(token) == {
        "link_status": "VALID",
        "privacy_policy_url": "https://www.yamaha.com.pe/politica-de-privacidad",
        "dealer_display_name": None,
    }

    submitted = await flow.customer_submits(token, ACCEPT)
    assert submitted.status_code == 200
    assert submitted.json() == {"status": "ACEPTADO"}

    # El callback llegó al mock de KIZUNA.
    callbacks = await flow.kizuna_received()
    assert list(callbacks) == ["TRX-OK-1"]
    callback = callbacks["TRX-OK-1"]
    assert {k: v for k, v in callback.items() if k != "responded_at"} == {
        "transaction_id": "TRX-OK-1",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "DEALER-001",
        "status": "ACEPTADO",
        "consents": {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
    }
    notification = (await flow.notifications())["TRX-OK-1"]
    assert (notification.status, notification.attempts) == (NotificationStatus.ENVIADO, 1)

    # La consulta de KIZUNA coincide con el callback, y el enlace ya no sirve.
    queried = await flow.kizuna_queries("TRX-OK-1")
    assert (queried["status"], queried["consents"], queried["responded_at"]) == (
        "ACEPTADO",
        callback["consents"],
        callback["responded_at"],
    )
    assert (await flow.customer_opens(token))["link_status"] == "USED"
    resubmitted = await flow.customer_submits(token, REJECT_ALL)
    assert (resubmitted.status_code, resubmitted.json()["link_status"]) == (410, "USED")
    assert len(await flow.kizuna_received()) == 1


# --- RN-01: todos los checks desmarcados ---


async def test_rn01_all_unchecked(flow: LocalFlow) -> None:
    _, token = await flow.kizuna_sends("TRX-RN01-1")
    assert (await flow.customer_opens(token))["link_status"] == "VALID"

    submitted = await flow.customer_submits(token, REJECT_ALL)
    # El rechazo se registra (no es un error) y la landing redirige a /gracias.
    assert submitted.status_code == 200
    assert submitted.json() == {"status": "NO_ACEPTADO"}

    callback = (await flow.kizuna_received())["TRX-RN01-1"]
    assert callback["status"] == "NO_ACEPTADO"
    assert callback["consents"] == {
        "personal_data": False,
        "marketing_yamaha": None,
        "marketing_dealer": None,
    }
    assert (callback["document_number"], callback["dealer"]) == ("12345678", "DEALER-001")

    queried = await flow.kizuna_queries("TRX-RN01-1")
    assert (queried["status"], queried["is_deleted"]) == ("NO_ACEPTADO", 1)
    assert queried["consents"] == callback["consents"]
    assert (await flow.notifications())["TRX-RN01-1"].status == NotificationStatus.ENVIADO


# --- RN-04: nuevo transaction_id con un registro activo ---


async def test_rn04_new_transaction_id_while_pending(flow: LocalFlow) -> None:
    _, token = await flow.kizuna_sends("TRX-RN04-1")

    # KIZUNA envía el mismo cliente y Dealer con otro ID: se actualizan los datos, sin WhatsApp ni enlace nuevos.
    response, new_token = await flow.kizuna_sends("TRX-RN04-2", phone="911 222 333")
    assert response.status_code == 200
    body = response.json()
    assert (body["result"], body["affected_transaction_id"], body["status"], body["whatsapp"]) == (
        "UPDATED",
        "TRX-RN04-1",
        "PENDIENTE",
        None,
    )
    assert new_token is None
    assert len(flow.sender.calls) == 1
    assert (await flow.kizuna_queries("TRX-RN04-2"))["status"] == "PENDIENTE"
    assert await flow.kizuna_received() == {}

    # El cliente responde con el enlace original.
    assert (await flow.customer_opens(token))["link_status"] == "VALID"
    assert (await flow.customer_submits(token, ACCEPT)).json() == {"status": "ACEPTADO"}

    # KIZUNA recibe el callback con el ID anterior (registro existente) y, además, uno con el ID nuevo.
    callbacks = await flow.kizuna_received()
    assert sorted(callbacks) == ["TRX-RN04-1", "TRX-RN04-2"]
    assert callbacks["TRX-RN04-1"]["status"] == "ACEPTADO"
    for transaction_id in ("TRX-RN04-1", "TRX-RN04-2"):
        assert callbacks[transaction_id]["consents"] == callbacks["TRX-RN04-1"]["consents"]
        assert callbacks[transaction_id]["responded_at"] == callbacks["TRX-RN04-1"]["responded_at"]
        assert (await flow.kizuna_queries(transaction_id))["status"] == "ACEPTADO"


async def test_rn04_new_transaction_id_after_acceptance(flow: LocalFlow) -> None:
    _, token = await flow.kizuna_sends("TRX-RN04A-1")
    await flow.customer_submits(token, ACCEPT)
    assert list(await flow.kizuna_received()) == ["TRX-RN04A-1"]

    # El ID nuevo llega cuando el cliente ya aceptó: nace con su respuesta y KIZUNA la recibe sin otro submit.
    response, new_token = await flow.kizuna_sends("TRX-RN04A-2")
    assert (response.status_code, response.json()["status"], new_token) == (200, "ACEPTADO", None)

    callbacks = await flow.kizuna_received()
    assert sorted(callbacks) == ["TRX-RN04A-1", "TRX-RN04A-2"]
    assert callbacks["TRX-RN04A-2"]["consents"] == callbacks["TRX-RN04A-1"]["consents"]
    assert callbacks["TRX-RN04A-2"]["responded_at"] == callbacks["TRX-RN04A-1"]["responded_at"]


# --- RN-05: nueva compra tras un rechazo ---


async def test_rn05_new_purchase_after_rejection(flow: LocalFlow, e2e_settings: Settings) -> None:
    _, first_token = await flow.kizuna_sends("TRX-RN05-1")
    assert (await flow.customer_submits(first_token, REJECT_ALL)).json() == {"status": "NO_ACEPTADO"}

    # Nueva compra del mismo cliente y Dealer: registro, enlace y WhatsApp nuevos.
    response, second_token = await flow.kizuna_sends("TRX-RN05-2")
    assert response.status_code == 201
    assert (response.json()["result"], response.json()["affected_transaction_id"]) == (
        "CREATED",
        "TRX-RN05-2",
    )
    assert second_token is not None and second_token != first_token
    await assert_link_generated(flow, "TRX-RN05-2", second_token, e2e_settings)
    assert len(flow.sender.calls) == 2

    # El enlace anterior ya no sirve; el nuevo sí, y esta vez el cliente acepta.
    assert (await flow.customer_opens(first_token))["link_status"] == "USED"
    assert (await flow.customer_opens(second_token))["link_status"] == "VALID"
    assert (await flow.customer_submits(second_token, ACCEPT)).json() == {"status": "ACEPTADO"}

    callbacks = await flow.kizuna_received()
    assert (callbacks["TRX-RN05-1"]["status"], callbacks["TRX-RN05-2"]["status"]) == (
        "NO_ACEPTADO",
        "ACEPTADO",
    )

    # La consulta por documento muestra ambos registros, del más reciente al más antiguo.
    listing = await flow.api.get(
        "/api/v1/consents",
        params={"document_type": "DNI", "document_number": "12345678", "dealer": "DEALER-001"},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert [(r["transaction_id"], r["status"], r["is_deleted"]) for r in listing.json()] == [
        ("TRX-RN05-2", "ACEPTADO", 0),
        ("TRX-RN05-1", "NO_ACEPTADO", 1),
    ]
