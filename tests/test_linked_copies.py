"""Copias vinculadas (RN-04 con un ID nuevo): reciben la respuesta del registro principal.

`answer` simula lo que hará M4 al registrar la respuesta del cliente: bloquea la fila del principal, guarda la
respuesta y llama a apply_response_to_linked_copies en la misma transacción.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.models import Consent, ConsentStatus
from app.services.linked_copies import apply_response_to_linked_copies
from tests.db_helpers import Sessions, accept, get_consent

URL = "/api/v1/consents"
BODY: dict[str, Any] = {
    "transaction_id": "TRX-1",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan pérez",
    "phone": "987654321",
    "dealer": "DEALER-001",
}
RESPONDED_AT = datetime(2026, 11, 5, 15, 20, 11, tzinfo=UTC)


async def post(client: httpx.AsyncClient, **overrides: Any) -> httpx.Response:
    response = await client.post(URL, json={**BODY, **overrides})
    assert response.status_code in (200, 201), response.text
    return response


def set_answer(
    primary: Consent, *, personal_data: bool, marketing_yamaha: bool, marketing_dealer: bool
) -> None:
    """Lo que M4 guarda en el principal (incluido RN-01 si personal_data es False)."""
    primary.responded_at = RESPONDED_AT
    primary.consent_personal_data = personal_data
    if personal_data:
        primary.status = ConsentStatus.ACEPTADO
        primary.consent_marketing_yamaha = marketing_yamaha
        primary.consent_marketing_dealer = marketing_dealer
    else:
        primary.status = ConsentStatus.NO_ACEPTADO
        primary.is_deleted = 1
        primary.deleted_at = RESPONDED_AT
        primary.full_name = None
        primary.consent_marketing_yamaha = None
        primary.consent_marketing_dealer = None


async def answer(
    sessions: Sessions,
    transaction_id: str,
    *,
    personal_data: bool = True,
    marketing_yamaha: bool = False,
    marketing_dealer: bool = True,
) -> list[str]:
    async with sessions.begin() as session:
        primary = await session.get(Consent, transaction_id, with_for_update=True)
        set_answer(
            primary,
            personal_data=personal_data,
            marketing_yamaha=marketing_yamaha,
            marketing_dealer=marketing_dealer,
        )
        copies = await apply_response_to_linked_copies(session, primary)
        return [copy.transaction_id for copy in copies]


async def test_pending_copies_receive_the_acceptance(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-2")
    await post(api_client, transaction_id="TRX-3")

    updated = await answer(session_factory, "TRX-1", marketing_yamaha=False, marketing_dealer=True)
    assert updated == ["TRX-2", "TRX-3"]

    for tid in ("TRX-2", "TRX-3"):
        copy = await get_consent(session_factory, tid)
        assert copy.status == ConsentStatus.ACEPTADO
        assert (copy.consent_personal_data, copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (
            True,
            False,
            True,
        )
        assert copy.responded_at == RESPONDED_AT
        assert copy.is_deleted == 0
        assert copy.full_name == "JUAN PÉREZ"


async def test_rejection_applies_rn01_to_each_copy(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-2", phone="911222333")

    assert await answer(session_factory, "TRX-1", personal_data=False) == ["TRX-2"]

    copy = await get_consent(session_factory, "TRX-2")
    assert copy.status == ConsentStatus.NO_ACEPTADO
    assert (copy.is_deleted, copy.full_name) == (1, None)
    assert copy.deleted_at == RESPONDED_AT
    assert (copy.consent_personal_data, copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (
        False,
        None,
        None,
    )
    # Se conservan documento, Dealer, teléfono y transacción.
    assert (copy.transaction_id, copy.document_number, copy.dealer, copy.phone) == (
        "TRX-2",
        "12345678",
        "DEALER-001",
        "911222333",
    )


async def test_after_rejection_a_new_purchase_is_a_new_primary(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-2")
    await answer(session_factory, "TRX-1", personal_data=False)

    response = await post(api_client, transaction_id="TRX-3")
    assert response.status_code == 201
    assert (await get_consent(session_factory, "TRX-3")).source_transaction_id is None


async def test_copies_already_answered_are_not_changed(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    # TRX-2 nace con la respuesta del principal; luego no hay nada pendiente que propagar.
    await post(api_client)
    await accept(session_factory, "TRX-1")
    await post(api_client, transaction_id="TRX-2")

    async with session_factory.begin() as session:
        primary = await session.get(Consent, "TRX-1", with_for_update=True)
        assert await apply_response_to_linked_copies(session, primary) == []
    assert (await get_consent(session_factory, "TRX-2")).status == ConsentStatus.ACEPTADO


async def test_copies_of_other_primaries_are_not_touched(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    await post(api_client, transaction_id="TRX-2")
    # Mismo documento con otro Dealer: otro principal con su propia copia.
    await post(api_client, transaction_id="TRX-D2", dealer="DEALER-002")
    await post(api_client, transaction_id="TRX-D2-COPY", dealer="DEALER-002")

    assert await answer(session_factory, "TRX-1") == ["TRX-2"]
    assert (await get_consent(session_factory, "TRX-D2-COPY")).status == ConsentStatus.PENDIENTE


async def test_primary_without_answer_is_an_error(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    async with session_factory() as session:
        primary = await session.get(Consent, "TRX-1")
        with pytest.raises(ValueError, match="todavía no tiene respuesta"):
            await apply_response_to_linked_copies(session, primary)


# --- Carreras entre M2 (nuevo ID) y M4 (respuesta del cliente) ---


async def test_new_id_arriving_while_the_customer_answers_gets_the_answer(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)

    async with session_factory.begin() as session:
        # M4 bloquea el principal y está registrando la respuesta...
        primary = await session.get(Consent, "TRX-1", with_for_update=True)
        # ...mientras KIZUNA envía un ID nuevo: el POST queda esperando el bloqueo.
        pending_post = asyncio.create_task(post(api_client, transaction_id="TRX-2"))
        await asyncio.sleep(0.3)
        assert not pending_post.done()

        set_answer(primary, personal_data=True, marketing_yamaha=True, marketing_dealer=False)
        assert await apply_response_to_linked_copies(session, primary) == []  # la copia aún no existe

    response = await pending_post
    assert response.status_code == 200
    # La copia nace con la respuesta ya registrada: no queda PENDIENTE.
    assert response.json()["status"] == "ACEPTADO"
    copy = await get_consent(session_factory, "TRX-2")
    assert copy.status == ConsentStatus.ACEPTADO
    assert (copy.consent_marketing_yamaha, copy.consent_marketing_dealer) == (True, False)


async def test_new_id_arriving_while_the_customer_rejects_creates_a_new_primary(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)

    async with session_factory.begin() as session:
        primary = await session.get(Consent, "TRX-1", with_for_update=True)
        pending_post = asyncio.create_task(post(api_client, transaction_id="TRX-2"))
        await asyncio.sleep(0.3)
        set_answer(primary, personal_data=False, marketing_yamaha=False, marketing_dealer=False)
        await apply_response_to_linked_copies(session, primary)

    # Tras el rechazo ya no hay principal activo: RN-05, registro y WhatsApp nuevos.
    response = await pending_post
    assert response.status_code == 201
    assert (await get_consent(session_factory, "TRX-2")).source_transaction_id is None


async def test_rows_are_consistent_after_concurrent_ids_and_answer(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await post(api_client)
    posts = [asyncio.create_task(post(api_client, transaction_id=f"TRX-N{i}")) for i in range(5)]
    await asyncio.sleep(0)
    await answer(session_factory, "TRX-1")
    await asyncio.gather(*posts)

    async with session_factory() as session:
        statuses = set(
            await session.scalars(select(Consent.status).where(Consent.source_transaction_id == "TRX-1"))
        )
    # Ninguna copia queda PENDIENTE: o nació con la respuesta o la recibió.
    assert statuses == {ConsentStatus.ACEPTADO}
