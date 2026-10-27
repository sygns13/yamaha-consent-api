"""Consultas de KIZUNA (M2, sección 2.4): GET /api/v1/consents/{transaction_id} y GET /api/v1/consents."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.models import Consent
from tests.db_helpers import Sessions, accept, reject, update_consent

URL = "/api/v1/consents"
CREATED_AT = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
RESPONDED_AT = datetime(2026, 11, 5, 15, 20, 11, tzinfo=UTC)
BODY: dict[str, Any] = {
    "transaction_id": "TRX-000123",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan  pérez   gómez",
    "phone": "+51 987 654 321",
    "dealer": "DEALER-001",
}
PERSONAL_DATA = ("JUAN", "PÉREZ", "987654321", "full_name", "phone", "additional_info")


async def create(
    client: httpx.AsyncClient, sessions: Sessions, *, created_at: datetime = CREATED_AT, **overrides: Any
) -> str:
    """Registra por POST (como KIZUNA) y fija created_at para que las pruebas no dependan del reloj."""
    body = {**BODY, **overrides}
    response = await client.post(URL, json=body)
    assert response.status_code == 201, response.text
    await update_consent(sessions, body["transaction_id"], created_at=created_at)
    return body["transaction_id"]


def assert_no_personal_data(response: httpx.Response) -> None:
    for value in PERSONAL_DATA:
        assert value not in response.text


# --- GET /api/v1/consents/{transaction_id} ---


async def test_get_pending_record(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await create(api_client, session_factory)

    response = await api_client.get(f"{URL}/TRX-000123")
    assert response.status_code == 200
    assert response.json() == {
        "transaction_id": "TRX-000123",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "DEALER-001",
        "status": "PENDIENTE",
        "consents": {"personal_data": None, "marketing_yamaha": None, "marketing_dealer": None},
        "responded_at": None,
        "is_deleted": 0,
        "created_at": "2026-11-04T10:00:00Z",
    }


async def test_get_accepted_record_matches_spec_example(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory)
    await accept(session_factory, "TRX-000123", responded_at=RESPONDED_AT)

    response = await api_client.get(f"{URL}/TRX-000123")
    # Ejemplo de la sección 2.4.
    assert response.json() == {
        "transaction_id": "TRX-000123",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "DEALER-001",
        "status": "ACEPTADO",
        "consents": {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
        "responded_at": "2026-11-05T15:20:11Z",
        "is_deleted": 0,
        "created_at": "2026-11-04T10:00:00Z",
    }


async def test_get_rejected_record_has_null_marketing(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory)
    await reject(session_factory, "TRX-000123")

    body = (await api_client.get(f"{URL}/TRX-000123")).json()
    assert body["status"] == "NO_ACEPTADO"
    assert body["is_deleted"] == 1
    assert body["consents"] == {"personal_data": False, "marketing_yamaha": None, "marketing_dealer": None}
    assert body["responded_at"] is not None
    # Documento, Dealer y transacción se conservan.
    assert (body["transaction_id"], body["document_number"], body["dealer"]) == (
        "TRX-000123",
        "12345678",
        "DEALER-001",
    )


async def test_deleted_record_hides_marketing_even_if_stored(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    # Datos inconsistentes a propósito: la respuesta igual oculta el marketing de un registro borrado.
    await create(api_client, session_factory)
    await update_consent(
        session_factory,
        "TRX-000123",
        is_deleted=1,
        consent_personal_data=False,
        consent_marketing_yamaha=True,
        consent_marketing_dealer=True,
    )
    body = (await api_client.get(f"{URL}/TRX-000123")).json()
    assert body["consents"] == {"personal_data": False, "marketing_yamaha": None, "marketing_dealer": None}


async def test_get_has_no_name_or_phone(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await create(api_client, session_factory, additional_info={"canal": "tienda"})
    response = await api_client.get(f"{URL}/TRX-000123")
    assert set(response.json()) == {
        "transaction_id",
        "document_type",
        "document_number",
        "dealer",
        "status",
        "consents",
        "responded_at",
        "is_deleted",
        "created_at",
    }
    assert_no_personal_data(response)
    assert "tienda" not in response.text


async def test_get_unknown_transaction_returns_404(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(f"{URL}/TRX-NO-EXISTE")
    assert response.status_code == 404
    assert response.json() == {"detail": "No existe un registro con ese ID de transacción"}


async def test_get_is_exact_on_transaction_id(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory)
    assert (await api_client.get(f"{URL}/trx-000123")).status_code == 404
    assert (await api_client.get(f"{URL}/TRX-000123%20")).status_code == 404


async def test_get_new_transaction_id_from_rn04_returns_its_linked_copy(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory)
    await accept(session_factory, "TRX-000123", responded_at=RESPONDED_AT)
    response = await api_client.post(URL, json={**BODY, "transaction_id": "TRX-000999"})
    assert response.json()["affected_transaction_id"] == "TRX-000123"
    await update_consent(session_factory, "TRX-000999", created_at=datetime(2026, 11, 6, tzinfo=UTC))

    # KIZUNA puede consultar con su propio ID: el registro tiene las respuestas que el cliente ya dio.
    response = await api_client.get(f"{URL}/TRX-000999")
    assert response.status_code == 200
    assert response.json() == {
        "transaction_id": "TRX-000999",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "DEALER-001",
        "status": "ACEPTADO",
        "consents": {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
        "responded_at": "2026-11-05T15:20:11Z",
        "is_deleted": 0,
        "created_at": "2026-11-06T00:00:00Z",
    }


async def test_list_includes_linked_copies(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await create(api_client, session_factory)
    await api_client.post(URL, json={**BODY, "transaction_id": "TRX-000999"})
    await update_consent(session_factory, "TRX-000999", created_at=datetime(2026, 11, 6, tzinfo=UTC))

    body = (
        await search(api_client, document_type="DNI", document_number="12345678", dealer="DEALER-001")
    ).json()
    assert [(r["transaction_id"], r["status"]) for r in body] == [
        ("TRX-000999", "PENDIENTE"),
        ("TRX-000123", "PENDIENTE"),
    ]


# --- GET /api/v1/consents?document_type=...&document_number=...&dealer=... ---


async def seed_history(client: httpx.AsyncClient, sessions: Sessions) -> None:
    """Mismo documento: dos compras con DEALER-001 (la primera rechazada) y una con DEALER-002."""
    await create(client, sessions, transaction_id="TRX-1", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    await reject(sessions, "TRX-1")
    await create(client, sessions, transaction_id="TRX-2", created_at=datetime(2026, 3, 1, tzinfo=UTC))
    await create(
        client,
        sessions,
        transaction_id="TRX-3",
        dealer="DEALER-002",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    # Otro cliente: no debe aparecer.
    await create(client, sessions, transaction_id="TRX-OTRO", document_number="87654321")


async def search(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    return await client.get(URL, params=params)


async def test_list_by_document_includes_all_dealers_newest_first(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)

    response = await search(api_client, document_type="DNI", document_number="12345678")
    assert response.status_code == 200
    body = response.json()
    assert [r["transaction_id"] for r in body] == ["TRX-2", "TRX-3", "TRX-1"]
    assert [r["created_at"] for r in body] == [
        "2026-03-01T00:00:00Z",
        "2026-02-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    ]


async def test_list_filtered_by_dealer_includes_deleted_records(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)

    body = (
        await search(api_client, document_type="DNI", document_number="12345678", dealer="DEALER-001")
    ).json()
    assert [(r["transaction_id"], r["is_deleted"], r["status"]) for r in body] == [
        ("TRX-2", 0, "PENDIENTE"),
        ("TRX-1", 1, "NO_ACEPTADO"),
    ]
    rejected = body[1]
    assert rejected["consents"] == {
        "personal_data": False,
        "marketing_yamaha": None,
        "marketing_dealer": None,
    }


async def test_list_normalizes_the_query_like_the_registration(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)
    body = (
        await search(api_client, document_type="dni", document_number="1234-5678", dealer=" DEALER-002 ")
    ).json()
    assert [r["transaction_id"] for r in body] == ["TRX-3"]


async def test_list_does_not_mix_document_types(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory, transaction_id="TRX-DNI")
    await create(api_client, session_factory, transaction_id="TRX-CE", document_type="CE")
    body = (await search(api_client, document_type="CE", document_number="12345678")).json()
    assert [r["transaction_id"] for r in body] == ["TRX-CE"]


async def test_list_ties_on_created_at_are_ordered_by_transaction_id(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory, transaction_id="TRX-A")
    await create(api_client, session_factory, transaction_id="TRX-B", dealer="DEALER-002")
    body = (await search(api_client, document_type="DNI", document_number="12345678")).json()
    assert [r["transaction_id"] for r in body] == ["TRX-B", "TRX-A"]


async def test_list_without_results_returns_empty_list(api_client: httpx.AsyncClient) -> None:
    response = await search(api_client, document_type="DNI", document_number="11111111")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_has_no_name_or_phone(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await seed_history(api_client, session_factory)
    response = await search(api_client, document_type="DNI", document_number="12345678")
    assert len(response.json()) == 3
    assert_no_personal_data(response)


@pytest.mark.parametrize(
    ("params", "error"),
    [
        (
            {"document_type": "DNI"},
            {"type": "missing", "loc": ["query", "document_number"], "msg": "Campo obligatorio"},
        ),
        (
            {"document_number": "12345678"},
            {"type": "missing", "loc": ["query", "document_type"], "msg": "Campo obligatorio"},
        ),
        (
            {"document_type": "DNI", "document_number": "123"},
            {
                "type": "value_error",
                "loc": ["query", "document_number"],
                "msg": "El DNI debe tener exactamente 8 dígitos",
            },
        ),
        (
            {"document_type": "LIBRETA", "document_number": "123"},
            {
                "type": "enum",
                "loc": ["query", "document_type"],
                "msg": "Debe ser uno de estos valores: 'DNI', 'RUC', 'CE', 'PASAPORTE' o 'OTROS'",
            },
        ),
        (
            {"document_type": "DNI", "document_number": "12345678", "dealer": "  "},
            {"type": "value_error", "loc": ["query", "dealer"], "msg": "El dealer no puede estar vacío"},
        ),
    ],
)
async def test_list_validation_errors(
    api_client: httpx.AsyncClient, params: dict[str, str], error: dict[str, Any]
) -> None:
    response = await search(api_client, **params)
    assert response.status_code == 422
    assert response.json() == {"detail": [error]}


# --- API Key ---


@pytest.mark.parametrize(
    ("path", "params"),
    [(f"{URL}/TRX-000123", {}), (URL, {"document_type": "DNI", "document_number": "12345678"})],
)
@pytest.mark.parametrize(("key", "detail"), [("", "Falta la API Key"), ("otra-clave", "API Key inválida")])
async def test_queries_require_api_key(
    api_client: httpx.AsyncClient, path: str, params: dict[str, str], key: str, detail: str
) -> None:
    response = await api_client.get(path, params=params, headers={"X-API-Key": key})
    assert response.status_code == 401
    assert response.json() == {"detail": detail}


async def test_queries_bypass_api_key_when_disabled(
    api_client: httpx.AsyncClient, session_factory: Sessions, test_settings: Settings
) -> None:
    await create(api_client, session_factory)
    test_settings.api_key_enabled = False
    headers = {"X-API-Key": ""}
    assert (await api_client.get(f"{URL}/TRX-000123", headers=headers)).status_code == 200
    response = await api_client.get(
        URL, params={"document_type": "DNI", "document_number": "12345678"}, headers=headers
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_queries_are_documented() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    detail = paths[f"{URL}/{{transaction_id}}"]["get"]
    listing = paths[URL]["get"]
    assert detail["security"] == listing["security"] == [{"ApiKey": []}]
    assert "404" in detail["responses"]
    params = {p["name"]: p["required"] for p in listing["parameters"]}
    assert params == {"document_type": True, "document_number": True, "dealer": False}


def test_consent_model_fields_not_exposed_are_personal_data() -> None:
    # Si se agrega una columna a Consent, este test obliga a decidir si se expone en las consultas.
    exposed = {"transaction_id", "document_type", "document_number", "dealer", "status", "responded_at"}
    exposed |= {"is_deleted", "created_at", "consent_personal_data", "consent_marketing_yamaha"}
    exposed |= {"consent_marketing_dealer"}
    # source_transaction_id no es dato personal, pero la respuesta de la sección 2.4 no lo incluye.
    hidden = {"full_name", "phone", "additional_info", "deleted_at", "updated_at", "source_transaction_id"}
    assert set(Consent.__table__.columns.keys()) == exposed | hidden
