"""POST /api/v1/consents (M2, sección 2.3): criterios de aceptación de M2 y casos borde.

Corre contra la base de pruebas (ver conftest.py) con un WhatsAppSender de prueba.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.integrations.whatsapp import WhatsAppSendResult
from app.models import (
    Consent,
    ConsentLink,
    ConsentRequest,
    ConsentStatus,
    WhatsappMessage,
    WhatsappStatus,
)
from app.utils.tokens import hash_token
from tests.conftest import RecordingWhatsAppSender
from tests.db_helpers import Sessions, accept, all_rows, count, get_consent, reject

URL = "/api/v1/consents"
BODY: dict[str, Any] = {
    "transaction_id": "TRX-000123",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan  pérez   gómez",
    "phone": "+51 987 654 321",
    "dealer": "DEALER-001",
    "additional_info": {"canal": "tienda"},
}


async def post(client: httpx.AsyncClient, **overrides: Any) -> httpx.Response:
    return await client.post(URL, json={**BODY, **overrides})


# --- Creación (201) ---


async def test_create_returns_201(api_client: httpx.AsyncClient) -> None:
    response = await post(api_client)
    assert response.status_code == 201
    assert response.json() == {
        "transaction_id": "TRX-000123",
        "result": "CREATED",
        "affected_transaction_id": "TRX-000123",
        "status": "PENDIENTE",
        "whatsapp": {"status": "queued", "error_code": None},
    }


async def test_create_stores_normalized_record_request_link_and_message(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)

    consent = await get_consent(session_factory, "TRX-000123")
    assert consent is not None
    assert (consent.document_type, consent.document_number, consent.dealer) == (
        "DNI",
        "12345678",
        "DEALER-001",
    )
    assert consent.full_name == "JUAN PÉREZ GÓMEZ"
    assert consent.phone == "987654321"
    assert consent.additional_info == {"canal": "tienda"}
    assert consent.status == ConsentStatus.PENDIENTE
    assert consent.is_deleted == 0
    assert consent.consent_personal_data is None
    assert consent.responded_at is None

    (request,) = await all_rows(session_factory, ConsentRequest)
    assert (request.transaction_id, request.consent_transaction_id, request.action) == (
        "TRX-000123",
        "TRX-000123",
        "CREATED",
    )

    (link,) = await all_rows(session_factory, ConsentLink)
    assert link.transaction_id == "TRX-000123"
    assert link.used_at is None
    assert link.expires_at - link.created_at == timedelta(hours=168)

    (message,) = await all_rows(session_factory, WhatsappMessage)
    assert message.transaction_id == "TRX-000123"
    assert message.link_id == link.id
    assert message.to_phone == "whatsapp:+51987654321"
    assert message.content_sid == "HXtest"
    assert message.status == WhatsappStatus.QUEUED
    assert message.error_code is None

    (call,) = whatsapp_sender.calls
    assert call["to"] == "whatsapp:+51987654321"
    assert call["transaction_id"] == "TRX-000123"


async def test_only_the_token_hash_is_stored(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    token = whatsapp_sender.calls[0]["token"]
    (link,) = await all_rows(session_factory, ConsentLink)

    assert len(token) >= 43  # token_urlsafe(32)
    assert link.token_hash == hash_token(token)
    assert len(link.token_hash) == 64
    assert token not in link.token_hash


async def test_link_ttl_comes_from_settings(
    api_client: httpx.AsyncClient, session_factory: Sessions, test_settings: Settings
) -> None:
    test_settings.link_ttl_hours = 2
    await post(api_client)
    (link,) = await all_rows(session_factory, ConsentLink)
    assert link.expires_at - link.created_at == timedelta(hours=2)


async def test_each_link_gets_a_different_token(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-2", dealer="DEALER-002")
    tokens = {call["token"] for call in whatsapp_sender.calls}
    hashes = {link.token_hash for link in await all_rows(session_factory, ConsentLink)}
    assert len(tokens) == 2
    assert hashes == {hash_token(t) for t in tokens}


# --- RN-04: mismo transaction_id (200) ---


async def test_rn04_same_transaction_id_updates_customer_data(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    response = await post(
        api_client, full_name="juan carlos pérez", phone="999 888 777", additional_info={"canal": "web"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "transaction_id": "TRX-000123",
        "result": "UPDATED",
        "affected_transaction_id": "TRX-000123",
        "status": "PENDIENTE",
        "whatsapp": None,
    }
    consent = await get_consent(session_factory, "TRX-000123")
    assert consent.full_name == "JUAN CARLOS PÉREZ"
    assert consent.phone == "999888777"
    assert consent.additional_info == {"canal": "web"}

    actions = [
        (r.transaction_id, r.consent_transaction_id, r.action)
        for r in await all_rows(session_factory, ConsentRequest)
    ]
    assert actions == [("TRX-000123", "TRX-000123", "CREATED"), ("TRX-000123", "TRX-000123", "UPDATED")]
    # Sin enlace ni WhatsApp nuevos.
    assert await count(session_factory, ConsentLink) == 1
    assert await count(session_factory, WhatsappMessage) == 1
    assert len(whatsapp_sender.calls) == 1


async def test_rn04_same_transaction_id_keeps_document_and_dealer_of_the_record(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    response = await post(
        api_client, document_number="87654321", dealer="DEALER-999", full_name="otro nombre"
    )
    assert response.status_code == 200
    consent = await get_consent(session_factory, "TRX-000123")
    assert (consent.document_number, consent.dealer) == ("12345678", "DEALER-001")
    assert consent.full_name == "OTRO NOMBRE"
    assert await count(session_factory, Consent) == 1


async def test_rn04_keeps_additional_info_when_not_sent(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    body = {k: v for k, v in BODY.items() if k != "additional_info"}
    assert (await api_client.post(URL, json=body)).status_code == 200
    assert (await get_consent(session_factory, "TRX-000123")).additional_info == {"canal": "tienda"}

    # Si se envía explícitamente null, se reemplaza.
    await post(api_client, additional_info=None)
    assert (await get_consent(session_factory, "TRX-000123")).additional_info is None


# --- RN-04: nuevo transaction_id con registro activo (200, sin WhatsApp) ---


async def test_rn04_new_transaction_id_with_active_record(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    response = await post(api_client, transaction_id="TRX-000999", full_name="juan pérez", phone="911222333")

    assert response.status_code == 200
    assert response.json() == {
        "transaction_id": "TRX-000999",
        "result": "UPDATED",
        "affected_transaction_id": "TRX-000123",
        "status": "PENDIENTE",
        "whatsapp": None,
    }
    # Se actualizan los datos del cliente en el registro principal (RN-04)...
    primary = await get_consent(session_factory, "TRX-000123")
    assert primary.full_name == "JUAN PÉREZ"
    assert primary.phone == "911222333"
    assert primary.source_transaction_id is None

    # ...y el ID nuevo se registra como copia vinculada, PENDIENTE como el principal.
    copy = await get_consent(session_factory, "TRX-000999")
    assert copy is not None
    assert copy.source_transaction_id == "TRX-000123"
    assert (copy.document_type, copy.document_number, copy.dealer) == ("DNI", "12345678", "DEALER-001")
    assert (copy.full_name, copy.phone) == ("JUAN PÉREZ", "911222333")
    assert copy.status == ConsentStatus.PENDIENTE
    assert copy.is_deleted == 0
    assert (copy.consent_personal_data, copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (
        None,
        None,
        None,
    )
    assert copy.responded_at is None

    last_request = (await all_rows(session_factory, ConsentRequest))[-1]
    assert (last_request.transaction_id, last_request.consent_transaction_id, last_request.action) == (
        "TRX-000999",
        "TRX-000123",
        "UPDATED",
    )
    # La copia no tiene enlace ni WhatsApp.
    assert len(whatsapp_sender.calls) == 1
    links = await all_rows(session_factory, ConsentLink)
    assert [link.transaction_id for link in links] == ["TRX-000123"]
    assert await count(session_factory, WhatsappMessage) == 1


async def test_rn04_copy_of_an_accepted_record_gets_the_answers(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    await accept(session_factory, "TRX-000123")

    response = await post(api_client, transaction_id="TRX-000999")
    assert response.status_code == 200
    assert response.json()["status"] == "ACEPTADO"
    assert response.json()["affected_transaction_id"] == "TRX-000123"

    primary = await get_consent(session_factory, "TRX-000123")
    copy = await get_consent(session_factory, "TRX-000999")
    # El principal no se toca...
    assert primary.status == ConsentStatus.ACEPTADO
    assert (
        primary.consent_personal_data,
        primary.consent_marketing_yamaha,
        primary.consent_marketing_dealer,
    ) == (
        True,
        False,
        True,
    )
    # ...y la copia nace con las mismas respuestas.
    assert copy.status == ConsentStatus.ACEPTADO
    assert (copy.consent_personal_data, copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (
        True,
        False,
        True,
    )
    assert copy.responded_at == primary.responded_at
    assert copy.source_transaction_id == "TRX-000123"


async def test_rn04_several_new_ids_link_to_the_primary_not_to_a_copy(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    for tid in ("TRX-A", "TRX-B", "TRX-C"):
        response = await post(api_client, transaction_id=tid)
        assert response.status_code == 200
        assert response.json()["affected_transaction_id"] == "TRX-000123"

    for tid in ("TRX-A", "TRX-B", "TRX-C"):
        assert (await get_consent(session_factory, tid)).source_transaction_id == "TRX-000123"
    assert await count(session_factory, Consent) == 4
    assert len(whatsapp_sender.calls) == 1


async def test_rn04_resending_a_linked_copy_updates_only_that_copy(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-000999")

    response = await post(api_client, transaction_id="TRX-000999", full_name="nombre corregido")
    assert response.status_code == 200
    assert response.json()["affected_transaction_id"] == "TRX-000999"
    assert (await get_consent(session_factory, "TRX-000999")).full_name == "NOMBRE CORREGIDO"
    assert (await get_consent(session_factory, "TRX-000123")).full_name == "JUAN PÉREZ GÓMEZ"
    assert await count(session_factory, Consent) == 2
    assert len(whatsapp_sender.calls) == 1


async def test_rn05_after_rejection_ignores_old_rejected_copies(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-COPY")
    # M4 rechaza el principal y propaga el rechazo a la copia.
    await reject(session_factory, "TRX-000123")
    await reject(session_factory, "TRX-COPY")

    response = await post(api_client, transaction_id="TRX-NEW")
    assert response.status_code == 201
    assert (await get_consent(session_factory, "TRX-NEW")).source_transaction_id is None
    assert len(whatsapp_sender.calls) == 2


async def test_active_copy_does_not_count_as_the_active_record(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    # Datos de borde: el principal fue rechazado pero su copia quedó activa (no debería pasar con M4).
    # Igual se crea un principal nuevo con su enlace: el cliente necesita un enlace para responder.
    await post(api_client)
    await post(api_client, transaction_id="TRX-COPY")
    await reject(session_factory, "TRX-000123")

    response = await post(api_client, transaction_id="TRX-NEW")
    assert response.status_code == 201
    assert len(whatsapp_sender.calls) == 2


async def test_rn04_document_is_matched_after_normalization(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    response = await post(
        api_client,
        transaction_id="TRX-2",
        document_type="dni",
        document_number="1234-5678",
        dealer=" DEALER-001 ",
    )
    assert response.status_code == 200
    assert response.json()["affected_transaction_id"] == "TRX-000123"
    copy = await get_consent(session_factory, "TRX-2")
    assert (copy.document_number, copy.dealer, copy.source_transaction_id) == (
        "12345678",
        "DEALER-001",
        "TRX-000123",
    )


# --- RN-03: mismo documento con otro Dealer (201) ---


async def test_rn03_same_document_other_dealer_creates_new_record(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    response = await post(api_client, transaction_id="TRX-000124", dealer="DEALER-002")

    assert response.status_code == 201
    assert response.json()["result"] == "CREATED"
    assert response.json()["affected_transaction_id"] == "TRX-000124"
    assert await count(session_factory, Consent) == 2
    assert await count(session_factory, ConsentLink) == 2
    assert await count(session_factory, WhatsappMessage) == 2
    assert [c["transaction_id"] for c in whatsapp_sender.calls] == ["TRX-000123", "TRX-000124"]


async def test_same_document_number_with_other_document_type_is_another_customer(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    response = await post(api_client, transaction_id="TRX-2", document_type="CE")
    assert response.status_code == 201
    assert await count(session_factory, Consent) == 2


# --- RN-05: nueva compra tras un rechazo (201) ---


async def test_rn05_new_transaction_after_rejection_creates_new_record_and_whatsapp(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    await reject(session_factory, "TRX-000123")

    response = await post(api_client, transaction_id="TRX-000200")
    assert response.status_code == 201
    assert response.json() == {
        "transaction_id": "TRX-000200",
        "result": "CREATED",
        "affected_transaction_id": "TRX-000200",
        "status": "PENDIENTE",
        "whatsapp": {"status": "queued", "error_code": None},
    }

    old = await get_consent(session_factory, "TRX-000123")
    new = await get_consent(session_factory, "TRX-000200")
    # El registro rechazado no se toca.
    assert (old.is_deleted, old.status, old.full_name) == (1, ConsentStatus.NO_ACEPTADO, None)
    assert (new.is_deleted, new.status, new.full_name) == (0, ConsentStatus.PENDIENTE, "JUAN PÉREZ GÓMEZ")
    assert (new.document_number, new.dealer) == (old.document_number, old.dealer)

    links = await all_rows(session_factory, ConsentLink)
    assert [link.transaction_id for link in links] == ["TRX-000123", "TRX-000200"]
    assert [c["transaction_id"] for c in whatsapp_sender.calls] == ["TRX-000123", "TRX-000200"]


async def test_rn05_then_rn04_updates_the_new_active_record(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    await reject(session_factory, "TRX-000123")
    await post(api_client, transaction_id="TRX-000200")

    response = await post(api_client, transaction_id="TRX-000300")
    assert response.status_code == 200
    assert response.json()["affected_transaction_id"] == "TRX-000200"


async def test_resending_rejected_transaction_id_returns_409_without_changes(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    await post(api_client)
    await reject(session_factory, "TRX-000123")
    requests_before = await count(session_factory, ConsentRequest)

    response = await post(api_client, full_name="nuevo nombre")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "La transacción ya existe y fue rechazada (NO_ACEPTADO); no se modificó ningún dato",
        "transaction_id": "TRX-000123",
        "status": "NO_ACEPTADO",
        "is_deleted": 1,
    }
    consent = await get_consent(session_factory, "TRX-000123")
    assert consent.full_name is None
    assert await count(session_factory, ConsentRequest) == requests_before
    assert await count(session_factory, Consent) == 1
    assert len(whatsapp_sender.calls) == 1


# --- Fallo del WhatsApp (no es 5xx) ---


async def test_whatsapp_failure_keeps_the_record(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    whatsapp_sender.result = WhatsAppSendResult(
        status=WhatsappStatus.FAILED, error_code="63016", error_message="Fuera de la ventana de 24 horas"
    )
    response = await post(api_client)

    assert response.status_code == 201
    assert response.json()["whatsapp"] == {"status": "failed", "error_code": "63016"}
    assert await get_consent(session_factory, "TRX-000123") is not None
    (message,) = await all_rows(session_factory, WhatsappMessage)
    assert (message.status, message.error_code, message.error_message) == (
        WhatsappStatus.FAILED,
        "63016",
        "Fuera de la ventana de 24 horas",
    )
    assert message.twilio_message_sid is None


async def test_unexpected_sender_exception_is_recorded_not_500(
    api_client: httpx.AsyncClient,
    session_factory: Sessions,
    whatsapp_sender: RecordingWhatsAppSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    whatsapp_sender.error = ConnectionError("no se pudo conectar con whatsapp:+51987654321")
    with caplog.at_level(logging.INFO):
        response = await post(api_client)

    assert response.status_code == 201
    assert response.json()["whatsapp"] == {"status": "failed", "error_code": None}
    (message,) = await all_rows(session_factory, WhatsappMessage)
    assert message.status == WhatsappStatus.FAILED
    assert message.error_message.startswith("ConnectionError")
    assert await count(session_factory, ConsentLink) == 1
    # El mensaje de la excepción (puede traer el teléfono) no va al log.
    assert "987654321" not in caplog.text


# --- Concurrencia ---


async def test_concurrent_new_transactions_for_same_customer_create_only_one_primary(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    responses = await asyncio.gather(*(post(api_client, transaction_id=f"TRX-C{i}") for i in range(8)))

    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 7 + [201]
    created = next(r.json() for r in responses if r.status_code == 201)
    assert {r.json()["affected_transaction_id"] for r in responses} == {created["transaction_id"]}
    # Un solo principal con enlace y WhatsApp; los otros 7 IDs son copias vinculadas a él.
    consents = await all_rows(session_factory, Consent)
    assert len(consents) == 8
    primaries = [c for c in consents if c.source_transaction_id is None]
    assert [p.transaction_id for p in primaries] == [created["transaction_id"]]
    assert {c.source_transaction_id for c in consents if c not in primaries} == {created["transaction_id"]}
    assert await count(session_factory, ConsentLink) == 1
    assert len(whatsapp_sender.calls) == 1
    assert await count(session_factory, ConsentRequest) == 8


async def test_concurrent_resends_of_the_same_transaction(
    api_client: httpx.AsyncClient, session_factory: Sessions, whatsapp_sender: RecordingWhatsAppSender
) -> None:
    responses = await asyncio.gather(*(post(api_client) for _ in range(6)))
    assert sorted(r.status_code for r in responses) == [200] * 5 + [201]
    assert await count(session_factory, Consent) == 1
    assert len(whatsapp_sender.calls) == 1


async def test_concurrent_requests_for_different_customers_do_not_block_each_other(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    responses = await asyncio.gather(
        *(post(api_client, transaction_id=f"TRX-D{i}", document_number=f"1000000{i}") for i in range(6))
    )
    assert all(r.status_code == 201 for r in responses)
    assert await count(session_factory, Consent) == 6


# --- Validaciones (422) ---


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"document_number": "1234567"}, "document_number"),
        ({"document_type": "LIBRETA"}, "document_type"),
        ({"phone": "123"}, "phone"),
        ({"full_name": "   "}, "full_name"),
        ({"dealer": ""}, "dealer"),
        ({"transaction_id": "T" * 101}, "transaction_id"),
    ],
)
async def test_validation_errors_return_422_and_write_nothing(
    api_client: httpx.AsyncClient,
    session_factory: Sessions,
    whatsapp_sender: RecordingWhatsAppSender,
    overrides: dict[str, Any],
    field: str,
) -> None:
    response = await post(api_client, **overrides)
    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["detail"]] == [["body", field]]
    assert await count(session_factory, Consent) == 0
    assert whatsapp_sender.calls == []


async def test_missing_body_returns_422(api_client: httpx.AsyncClient) -> None:
    response = await api_client.post(URL)
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "El cuerpo de la petición es obligatorio"


# --- API Key (401) y bypass ---


async def test_missing_api_key_returns_401(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    response = await api_client.post(URL, json=BODY, headers={"X-API-Key": ""})
    assert response.status_code == 401
    assert response.json() == {"detail": "Falta la API Key"}
    assert await count(session_factory, Consent) == 0


async def test_invalid_api_key_returns_401(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    response = await api_client.post(URL, json=BODY, headers={"X-API-Key": "otra-clave"})
    assert response.status_code == 401
    assert response.json() == {"detail": "API Key inválida"}
    assert await count(session_factory, Consent) == 0


async def test_api_key_is_checked_before_body_validation(api_client: httpx.AsyncClient) -> None:
    # Sin clave no se revela nada sobre el formato esperado del body.
    response = await api_client.post(URL, json={}, headers={"X-API-Key": "otra-clave"})
    assert response.status_code == 401


async def test_api_key_bypass_when_disabled(
    api_client: httpx.AsyncClient, session_factory: Sessions, test_settings: Settings
) -> None:
    test_settings.api_key_enabled = False
    response = await api_client.post(URL, json=BODY, headers={"X-API-Key": ""})
    assert response.status_code == 201
    assert await count(session_factory, Consent) == 1


# --- Datos personales ---


async def test_responses_and_logs_have_no_personal_data(
    api_client: httpx.AsyncClient,
    session_factory: Sessions,
    whatsapp_sender: RecordingWhatsAppSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        created = await post(api_client)
        updated = await post(api_client, transaction_id="TRX-2")

    token = whatsapp_sender.calls[0]["token"]
    for text in (created.text, updated.text, caplog.text):
        for secret in ("12345678", "987654321", "JUAN", "PÉREZ", token):
            assert secret not in text


def test_route_is_documented_with_api_key_and_responses() -> None:
    from app.main import app

    operation = app.openapi()["paths"][URL]["post"]
    assert operation["security"] == [{"ApiKey": []}]
    assert {"200", "201", "409", "422"} <= set(operation["responses"])
