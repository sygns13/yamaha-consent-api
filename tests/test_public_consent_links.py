"""M4: API pública para la landing (validar enlace y registrar respuestas, RN-01)."""

import asyncio
import ipaddress
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from app.core.config import Settings
from app.models import (
    ConsentEvidence,
    ConsentLink,
    ConsentStatus,
    KizunaNotification,
    NotificationStatus,
    WhatsappMessage,
)
from app.utils.tokens import hash_token
from tests.conftest import RecordingWhatsAppSender
from tests.db_helpers import Sessions, all_rows, count, get_consent

CONSENTS_URL = "/api/v1/consents"
BODY: dict[str, Any] = {
    "transaction_id": "TRX-1",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan pérez",
    "phone": "987654321",
    "dealer": "DEALER-001",
    "additional_info": {"canal": "tienda"},
}
POLICY_URL = "https://www.yamaha.com.pe/politica-de-privacidad"
ACCEPT_ALL = {"personal_data": True, "marketing_yamaha": True, "marketing_dealer": True}
REJECT_ALL = {"personal_data": False, "marketing_yamaha": False, "marketing_dealer": False}
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"


def link_url(token: str) -> str:
    return f"/api/v1/public/consent-links/{token}"


def submit_url(token: str) -> str:
    return f"{link_url(token)}/submit"


@pytest.fixture(autouse=True)
def policy_settings(test_settings: Settings) -> None:
    test_settings.privacy_policy_url = POLICY_URL
    test_settings.privacy_policy_version = "v2"
    test_settings.kizuna_callback_url = "http://kizuna-mock:8081/kizuna/consent-result"


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[list[uuid.UUID]]:
    """Registra cada disparo de notificaciones (el envío real es M5)."""
    calls: list[list[uuid.UUID]] = []

    async def fake_dispatch(notification_ids: list[uuid.UUID]) -> None:
        calls.append(list(notification_ids))

    monkeypatch.setattr("app.api.v1.public.dispatch_notifications", fake_dispatch)
    return calls


@pytest.fixture
async def public_client(api_client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """La landing no envía API Key: los endpoints públicos no la piden."""
    api_client.headers.pop("X-API-Key")
    yield api_client


async def create_consent(client: httpx.AsyncClient, sender: RecordingWhatsAppSender, **overrides: Any) -> str:
    """Registra un cliente por la API de KIZUNA y devuelve el token del enlace enviado por WhatsApp."""
    response = await client.post(
        CONSENTS_URL, json={**BODY, **overrides}, headers={"X-API-Key": "test-api-key"}
    )
    assert response.status_code in (200, 201), response.text
    return sender.calls[-1]["token"]


async def set_link(sessions: Sessions, token: str, **values: Any) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(ConsentLink).where(ConsentLink.token_hash == hash_token(token)).values(**values)
        )


async def submit(
    client: httpx.AsyncClient, token: str, answers: dict[str, bool], **kwargs: Any
) -> httpx.Response:
    return await client.post(submit_url(token), json=answers, **kwargs)


@pytest.fixture
async def token(public_client: httpx.AsyncClient, whatsapp_sender: RecordingWhatsAppSender) -> str:
    return await create_consent(public_client, whatsapp_sender)


# --- GET /consent-links/{token} ---


async def test_valid_link(public_client: httpx.AsyncClient, token: str) -> None:
    response = await public_client.get(link_url(token))
    assert response.status_code == 200
    # Sin datos personales: solo estos tres campos.
    assert response.json() == {
        "link_status": "VALID",
        "privacy_policy_url": POLICY_URL,
        "dealer_display_name": None,
    }


async def test_privacy_policy_url_not_configured_is_null(
    public_client: httpx.AsyncClient, token: str, test_settings: Settings
) -> None:
    test_settings.privacy_policy_url = ""
    assert (await public_client.get(link_url(token))).json()["privacy_policy_url"] is None


async def test_unknown_token_is_404(public_client: httpx.AsyncClient, token: str) -> None:
    response = await public_client.get(link_url("token-que-no-existe"))
    assert response.status_code == 404
    assert response.json() == {"detail": "El enlace no existe"}


async def test_expired_link(public_client: httpx.AsyncClient, token: str, session_factory: Sessions) -> None:
    await set_link(session_factory, token, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    response = await public_client.get(link_url(token))
    assert response.status_code == 200
    assert response.json()["link_status"] == "EXPIRED"


async def test_link_expires_after_link_ttl_hours(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, test_settings: Settings
) -> None:
    (link,) = await all_rows(session_factory, ConsentLink)
    assert link.expires_at - link.created_at == timedelta(hours=test_settings.link_ttl_hours)


async def test_used_link(public_client: httpx.AsyncClient, token: str, dispatched: list) -> None:
    await submit(public_client, token, ACCEPT_ALL)
    assert (await public_client.get(link_url(token))).json()["link_status"] == "USED"


async def test_used_and_expired_link_is_used(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions
) -> None:
    now = datetime.now(UTC)
    await set_link(
        session_factory, token, used_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)
    )
    assert (await public_client.get(link_url(token))).json()["link_status"] == "USED"


async def test_get_does_not_modify_anything(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions
) -> None:
    await public_client.get(link_url(token))
    (link,) = await all_rows(session_factory, ConsentLink)
    assert link.used_at is None
    assert await count(session_factory, ConsentEvidence) == 0


async def test_link_is_looked_up_by_hash(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions
) -> None:
    # El hash guardado no sirve como token.
    (link,) = await all_rows(session_factory, ConsentLink)
    assert link.token_hash == hash_token(token)
    assert (await public_client.get(link_url(link.token_hash))).status_code == 404


# --- POST /consent-links/{token}/submit: aceptación ---


async def test_full_acceptance(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    response = await submit(public_client, token, ACCEPT_ALL)
    assert response.status_code == 200
    assert response.json() == {"status": "ACEPTADO"}

    consent = await get_consent(session_factory, "TRX-1")
    assert consent.status == ConsentStatus.ACEPTADO
    assert (
        consent.consent_personal_data,
        consent.consent_marketing_yamaha,
        consent.consent_marketing_dealer,
    ) == (
        True,
        True,
        True,
    )
    assert consent.responded_at is not None
    assert (consent.is_deleted, consent.deleted_at) == (0, None)
    assert consent.full_name == "JUAN PÉREZ"

    (link,) = await all_rows(session_factory, ConsentLink)
    assert link.used_at == consent.responded_at


async def test_acceptance_with_marketing_false(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    answers = {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": False}
    response = await submit(public_client, token, answers)
    assert response.json() == {"status": "ACEPTADO"}

    consent = await get_consent(session_factory, "TRX-1")
    assert consent.status == ConsentStatus.ACEPTADO
    # false se guarda como false (no como null): el cliente respondió que no.
    assert (
        consent.consent_personal_data,
        consent.consent_marketing_yamaha,
        consent.consent_marketing_dealer,
    ) == (
        True,
        False,
        False,
    )
    assert consent.is_deleted == 0


async def test_acceptance_with_mixed_marketing(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await submit(
        public_client, token, {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}
    )
    consent = await get_consent(session_factory, "TRX-1")
    assert (consent.consent_marketing_yamaha, consent.consent_marketing_dealer) == (False, True)


# --- RN-01 ---


async def test_rn01_all_unchecked(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    response = await submit(public_client, token, REJECT_ALL)
    # El rechazo no es un error: se registra y la landing redirige a /gracias.
    assert response.status_code == 200
    assert response.json() == {"status": "NO_ACEPTADO"}

    consent = await get_consent(session_factory, "TRX-1")
    assert consent.status == ConsentStatus.NO_ACEPTADO
    assert consent.consent_personal_data is False
    # Borrado lógico: solo se anula el nombre; el marketing no se conserva.
    assert consent.full_name is None
    assert (consent.consent_marketing_yamaha, consent.consent_marketing_dealer) == (None, None)
    assert consent.is_deleted == 1
    assert consent.deleted_at == consent.responded_at
    # Se conservan transacción, documento, Dealer, teléfono e información adicional.
    assert (consent.transaction_id, consent.document_type.value, consent.document_number, consent.dealer) == (
        "TRX-1",
        "DNI",
        "12345678",
        "DEALER-001",
    )
    assert consent.phone == "987654321"
    assert consent.additional_info == {"canal": "tienda"}
    # La evidencia y el WhatsApp no se tocan.
    assert await count(session_factory, ConsentEvidence) == 1
    assert await count(session_factory, WhatsappMessage) == 1


async def test_rn01_with_marketing_checked_does_not_keep_marketing(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    answers = {"personal_data": False, "marketing_yamaha": True, "marketing_dealer": True}
    assert (await submit(public_client, token, answers)).json() == {"status": "NO_ACEPTADO"}

    consent = await get_consent(session_factory, "TRX-1")
    assert (consent.consent_marketing_yamaha, consent.consent_marketing_dealer) == (None, None)
    assert (consent.is_deleted, consent.full_name) == (1, None)


async def test_rn01_query_shows_marketing_null(
    public_client: httpx.AsyncClient, token: str, dispatched: list
) -> None:
    await submit(
        public_client, token, {"personal_data": False, "marketing_yamaha": True, "marketing_dealer": True}
    )
    response = await public_client.get(f"{CONSENTS_URL}/TRX-1", headers={"X-API-Key": "test-api-key"})
    assert response.json()["status"] == "NO_ACEPTADO"
    assert response.json()["consents"] == {
        "personal_data": False,
        "marketing_yamaha": None,
        "marketing_dealer": None,
    }
    assert response.json()["is_deleted"] == 1


async def test_after_rn01_a_new_purchase_creates_a_new_record(
    public_client: httpx.AsyncClient,
    token: str,
    session_factory: Sessions,
    whatsapp_sender: RecordingWhatsAppSender,
    dispatched: list,
) -> None:
    await submit(public_client, token, REJECT_ALL)
    new_token = await create_consent(public_client, whatsapp_sender, transaction_id="TRX-2")
    # RN-05: registro, enlace y WhatsApp nuevos; el cliente puede aceptar con el enlace nuevo.
    assert new_token != token
    assert (await submit(public_client, new_token, ACCEPT_ALL)).json() == {"status": "ACEPTADO"}
    assert (await get_consent(session_factory, "TRX-1")).status == ConsentStatus.NO_ACEPTADO


# --- Evidencia ---


async def test_evidence_is_recorded(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    answers = {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}
    await submit(
        public_client,
        token,
        answers,
        headers={"X-Forwarded-For": "190.236.10.20, 10.0.0.1", "User-Agent": USER_AGENT},
    )

    (evidence,) = await all_rows(session_factory, ConsentEvidence)
    (link,) = await all_rows(session_factory, ConsentLink)
    consent = await get_consent(session_factory, "TRX-1")
    assert evidence.transaction_id == "TRX-1"
    assert evidence.link_id == link.id
    assert ipaddress.ip_address(evidence.ip_address) == ipaddress.ip_address("190.236.10.20")
    assert evidence.user_agent == USER_AGENT
    assert (evidence.policy_version, evidence.policy_url) == ("v2", POLICY_URL)
    assert evidence.answers == answers
    assert evidence.submitted_at == consent.responded_at


async def test_evidence_keeps_what_the_customer_sent_in_rn01(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    answers = {"personal_data": False, "marketing_yamaha": True, "marketing_dealer": False}
    await submit(public_client, token, answers)
    (evidence,) = await all_rows(session_factory, ConsentEvidence)
    assert evidence.answers == answers


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        ("190.236.10.20", "190.236.10.20"),
        ("  190.236.10.20 ,130.211.0.1", "190.236.10.20"),
        ("2800:200:e840::1, 130.211.0.1", "2800:200:e840::1"),
        ("no-es-una-ip", None),
    ],
)
async def test_evidence_ip_from_x_forwarded_for(
    public_client: httpx.AsyncClient,
    token: str,
    session_factory: Sessions,
    dispatched: list,
    forwarded_for: str,
    expected: str | None,
) -> None:
    response = await submit(public_client, token, ACCEPT_ALL, headers={"X-Forwarded-For": forwarded_for})
    # Una IP inválida no impide registrar la respuesta: se guarda null.
    assert response.status_code == 200
    (evidence,) = await all_rows(session_factory, ConsentEvidence)
    stored = None if evidence.ip_address is None else str(ipaddress.ip_address(evidence.ip_address))
    assert stored == expected


async def test_evidence_ip_without_x_forwarded_for_uses_the_connection(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await submit(public_client, token, ACCEPT_ALL)
    (evidence,) = await all_rows(session_factory, ConsentEvidence)
    # httpx.ASGITransport simula la conexión desde 127.0.0.1.
    assert str(ipaddress.ip_address(evidence.ip_address)) == "127.0.0.1"


# --- Enlace vencido, usado o inexistente ---


async def test_expired_link_is_410(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await set_link(session_factory, token, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    response = await submit(public_client, token, ACCEPT_ALL)
    assert response.status_code == 410
    assert response.json() == {"detail": "El enlace venció", "link_status": "EXPIRED"}
    await assert_nothing_recorded(session_factory)
    assert dispatched == []


async def test_used_link_is_410(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    assert (await submit(public_client, token, ACCEPT_ALL)).status_code == 200
    response = await submit(public_client, token, REJECT_ALL)
    assert response.status_code == 410
    assert response.json() == {"detail": "El enlace ya fue utilizado", "link_status": "USED"}

    # La primera respuesta no cambia.
    consent = await get_consent(session_factory, "TRX-1")
    assert (consent.status, consent.is_deleted) == (ConsentStatus.ACEPTADO, 0)
    assert await count(session_factory, ConsentEvidence) == 1
    assert await count(session_factory, KizunaNotification) == 1
    assert len(dispatched) == 1


async def test_unknown_token_submit_is_404(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    response = await submit(public_client, "token-que-no-existe", ACCEPT_ALL)
    assert response.status_code == 404
    assert response.json() == {"detail": "El enlace no existe"}
    await assert_nothing_recorded(session_factory)


async def assert_nothing_recorded(sessions: Sessions) -> None:
    consent = await get_consent(sessions, "TRX-1")
    assert consent.status == ConsentStatus.PENDIENTE
    assert consent.responded_at is None
    (link,) = await all_rows(sessions, ConsentLink)
    assert link.used_at is None
    assert await count(sessions, ConsentEvidence) == 0
    assert await count(sessions, KizunaNotification) == 0


# --- Doble envío concurrente ---


async def test_concurrent_double_submit_records_only_one(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    responses = await asyncio.gather(
        submit(public_client, token, ACCEPT_ALL),
        submit(public_client, token, REJECT_ALL),
    )

    codes = sorted(response.status_code for response in responses)
    assert codes == [200, 410]
    winner = next(response for response in responses if response.status_code == 200)
    loser = next(response for response in responses if response.status_code == 410)
    assert loser.json()["link_status"] == "USED"

    # Solo uno se registra, y el registro queda con la respuesta del que ganó.
    assert await count(session_factory, ConsentEvidence) == 1
    assert await count(session_factory, KizunaNotification) == 1
    consent = await get_consent(session_factory, "TRX-1")
    assert consent.status.value == winner.json()["status"]
    (evidence,) = await all_rows(session_factory, ConsentEvidence)
    assert evidence.answers == (ACCEPT_ALL if winner.json()["status"] == "ACEPTADO" else REJECT_ALL)
    assert len(dispatched) == 1


async def test_many_concurrent_submits_record_only_one(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    responses = await asyncio.gather(*(submit(public_client, token, ACCEPT_ALL) for _ in range(5)))
    assert sorted(response.status_code for response in responses) == [200, 410, 410, 410, 410]
    assert await count(session_factory, ConsentEvidence) == 1
    assert await count(session_factory, KizunaNotification) == 1


# --- Validación del body ---


@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ({"personal_data": True, "marketing_yamaha": False}, ["body", "marketing_dealer"], "missing"),
        ({**ACCEPT_ALL, "personal_data": "true"}, ["body", "personal_data"], "bool_type"),
        ({**ACCEPT_ALL, "marketing_yamaha": 1}, ["body", "marketing_yamaha"], "bool_type"),
        ({**ACCEPT_ALL, "marketing_dealer": None}, ["body", "marketing_dealer"], "bool_type"),
        ({**ACCEPT_ALL, "full_name": "x"}, ["body", "full_name"], "extra_forbidden"),
    ],
)
async def test_invalid_body_is_422(
    public_client: httpx.AsyncClient,
    token: str,
    session_factory: Sessions,
    body: dict[str, Any],
    loc: list[str],
    error_type: str,
) -> None:
    response = await public_client.post(submit_url(token), json=body)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert {"type": error_type, "loc": loc} in [{"type": e["type"], "loc": e["loc"]} for e in errors]
    await assert_nothing_recorded(session_factory)


async def test_missing_body_is_422(public_client: httpx.AsyncClient, token: str) -> None:
    response = await public_client.post(submit_url(token))
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "El cuerpo de la petición es obligatorio"


# --- Notificaciones a KIZUNA (paso 8; el envío es M5) ---


async def test_acceptance_creates_a_pending_notification_and_dispatches_it(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await submit(
        public_client, token, {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}
    )

    (notification,) = await all_rows(session_factory, KizunaNotification)
    consent = await get_consent(session_factory, "TRX-1")
    assert notification.transaction_id == "TRX-1"
    assert notification.status == NotificationStatus.PENDIENTE
    assert notification.attempts == 0
    assert notification.target_url == "http://kizuna-mock:8081/kizuna/consent-result"
    assert notification.payload == {
        "transaction_id": "TRX-1",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "DEALER-001",
        "status": "ACEPTADO",
        "consents": {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
        "responded_at": consent.responded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    # Se dispara después del commit con el ID de la notificación.
    assert dispatched == [[notification.id]]


async def test_rejection_notification_has_marketing_null(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await submit(
        public_client, token, {"personal_data": False, "marketing_yamaha": True, "marketing_dealer": True}
    )
    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert notification.payload["status"] == "NO_ACEPTADO"
    assert notification.payload["consents"] == {
        "personal_data": False,
        "marketing_yamaha": None,
        "marketing_dealer": None,
    }
    # El payload no lleva nombre ni teléfono.
    assert not {"full_name", "phone"} & notification.payload.keys()


async def test_dispatch_runs_after_the_commit(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[NotificationStatus] = []

    async def check_committed(notification_ids: list[uuid.UUID]) -> None:
        # Otra sesión ya ve la notificación: la transacción del envío terminó.
        async with session_factory() as session:
            seen.extend([(await session.get(KizunaNotification, nid)).status for nid in notification_ids])

    monkeypatch.setattr("app.api.v1.public.dispatch_notifications", check_committed)
    await submit(public_client, token, ACCEPT_ALL)
    assert seen == [NotificationStatus.PENDIENTE]


async def test_default_dispatch_sends_nothing_yet(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions
) -> None:
    # Sin el reemplazo de prueba: el disparo real (TODO(M5)) no hace nada y la notificación queda PENDIENTE.
    assert (await submit(public_client, token, ACCEPT_ALL)).status_code == 200
    (notification,) = await all_rows(session_factory, KizunaNotification)
    assert (notification.status, notification.attempts) == (NotificationStatus.PENDIENTE, 0)


# --- Copias vinculadas (RN-04) ---


async def create_copies(client: httpx.AsyncClient, *transaction_ids: str) -> None:
    for tid in transaction_ids:
        response = await client.post(
            CONSENTS_URL, json={**BODY, "transaction_id": tid}, headers={"X-API-Key": "test-api-key"}
        )
        assert response.status_code == 200, response.text


async def test_acceptance_is_copied_to_pending_linked_copies(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await create_copies(public_client, "TRX-2", "TRX-3")
    await submit(
        public_client, token, {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}
    )

    primary = await get_consent(session_factory, "TRX-1")
    for tid in ("TRX-2", "TRX-3"):
        copy = await get_consent(session_factory, tid)
        assert copy.status == ConsentStatus.ACEPTADO
        assert (copy.consent_personal_data, copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (
            True,
            False,
            True,
        )
        assert copy.responded_at == primary.responded_at
        assert copy.is_deleted == 0

    # Una notificación por cada ID de transacción, cada una con su propio transaction_id.
    notifications = await all_rows(session_factory, KizunaNotification)
    assert sorted(n.transaction_id for n in notifications) == ["TRX-1", "TRX-2", "TRX-3"]
    assert all(n.payload["transaction_id"] == n.transaction_id for n in notifications)
    assert all(n.payload["status"] == "ACEPTADO" for n in notifications)
    assert sorted(dispatched[0]) == sorted(n.id for n in notifications)


async def test_rejection_applies_rn01_to_pending_linked_copies(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    await create_copies(public_client, "TRX-2")
    await submit(
        public_client, token, {"personal_data": False, "marketing_yamaha": True, "marketing_dealer": True}
    )

    copy = await get_consent(session_factory, "TRX-2")
    assert copy.status == ConsentStatus.NO_ACEPTADO
    assert (copy.is_deleted, copy.full_name) == (1, None)
    assert (copy.consent_personal_data, copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (
        False,
        None,
        None,
    )
    assert (copy.document_number, copy.dealer, copy.phone) == ("12345678", "DEALER-001", "987654321")

    notifications = await all_rows(session_factory, KizunaNotification)
    assert sorted(n.transaction_id for n in notifications) == ["TRX-1", "TRX-2"]
    assert all(n.payload["consents"]["marketing_yamaha"] is None for n in notifications)


async def test_new_id_arriving_during_submit_ends_answered(
    public_client: httpx.AsyncClient, token: str, session_factory: Sessions, dispatched: list
) -> None:
    # M2 (ID nuevo) y M4 (respuesta) a la vez: la copia nunca queda PENDIENTE.
    _, response = await asyncio.gather(
        submit(public_client, token, ACCEPT_ALL),
        public_client.post(
            CONSENTS_URL, json={**BODY, "transaction_id": "TRX-2"}, headers={"X-API-Key": "test-api-key"}
        ),
    )
    assert response.status_code == 200
    assert (await get_consent(session_factory, "TRX-2")).status == ConsentStatus.ACEPTADO


# --- Seguridad ---


async def test_public_endpoints_need_no_api_key(
    public_client: httpx.AsyncClient, token: str, dispatched: list
) -> None:
    assert "X-API-Key" not in public_client.headers
    assert (await public_client.get(link_url(token))).status_code == 200
    assert (await submit(public_client, token, ACCEPT_ALL)).status_code == 200


async def test_logs_never_contain_token_ip_or_personal_data(
    public_client: httpx.AsyncClient,
    token: str,
    caplog: pytest.LogCaptureFixture,
    dispatched: list,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await public_client.get(link_url(token))
        await submit(public_client, token, ACCEPT_ALL, headers={"X-Forwarded-For": "190.236.10.20"})
        await submit(public_client, token, ACCEPT_ALL, headers={"X-Forwarded-For": "190.236.10.20"})

    # El logger "httpx" es el del cliente de prueba (escribe la URL que pide), no el de la aplicación.
    logged = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records if record.name != "httpx"
    )
    for secret in (token, hash_token(token), "190.236.10.20", "987654321", "12345678", "JUAN"):
        assert secret not in logged


async def test_openapi_public_endpoints_have_no_security(public_client: httpx.AsyncClient) -> None:
    spec = (await public_client.get("/openapi.json")).json()
    for path in ("/api/v1/public/consent-links/{token}", "/api/v1/public/consent-links/{token}/submit"):
        for operation in spec["paths"][path].values():
            assert "security" not in operation


def test_link_status_rules() -> None:
    from app.services.consent_links import link_status

    now = datetime.now(UTC)
    valid = ConsentLink(expires_at=now + timedelta(hours=1), used_at=None)
    assert link_status(valid, now).value == "VALID"
    # Vence cuando now > expires_at (en el instante exacto todavía es válido).
    assert link_status(ConsentLink(expires_at=now, used_at=None), now).value == "VALID"
    assert (
        link_status(ConsentLink(expires_at=now, used_at=None), now + timedelta(microseconds=1)).value
        == "EXPIRED"
    )
    assert link_status(ConsentLink(expires_at=now - timedelta(days=1), used_at=now), now).value == "USED"
