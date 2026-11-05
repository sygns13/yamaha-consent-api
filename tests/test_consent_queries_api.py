"""Consultas de KIZUNA (M2, sección 2.4): GET /api/v1/consents/{IdentifierId} y GET /api/v1/consents."""

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
    "IdentifierId": "TRX-000123",
    "customerId": "12345678",
    "customerName": "juan  pérez   gómez",
    "customerPhone": "+51 987 654 321",
    "customerEmail": "juan.perez@mail.com",
    "dealerName": "ABC Motors",
    "dealerCode": "DEALER-001",
    "requestDateTime": "2026-11-04T04:59:00-05:00",
}
PERSONAL_DATA = ("JUAN", "PÉREZ", "987654321", "juan.perez", "customerName", "customerPhone", "customerEmail")


async def create(
    client: httpx.AsyncClient, sessions: Sessions, *, created_at: datetime = CREATED_AT, **overrides: Any
) -> str:
    """Registra por POST (como KIZUNA) y fija created_at para que las pruebas no dependan del reloj."""
    body = {**BODY, **overrides}
    response = await client.post(URL, json={"_request": body})
    assert response.status_code == 201, response.text
    await update_consent(sessions, body["IdentifierId"], created_at=created_at)
    return body["IdentifierId"]


def assert_no_personal_data(response: httpx.Response) -> None:
    for value in PERSONAL_DATA:
        assert value not in response.text


def detail(**overrides: Any) -> dict[str, Any]:
    """Respuesta esperada de un registro: fechas con el offset de Lima (KIZUNA_TIMEZONE)."""
    return {
        "IdentifierId": "TRX-000123",
        "customerId": "12345678",
        "dealerCode": "DEALER-001",
        "dealerName": "ABC Motors",
        "status": "PENDIENTE",
        "personalDataConsent": None,
        "marketingConsentYMDP": None,
        "marketingConsentDEALER": None,
        "consentDateTime": None,
        "requestDateTime": "2026-11-04T04:59:00-05:00",
        "isDeleted": 0,
        "createdAt": "2026-11-04T05:00:00-05:00",
        **overrides,
    }


# --- GET /api/v1/consents/{IdentifierId} ---


async def test_get_pending_record(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await create(api_client, session_factory)

    response = await api_client.get(f"{URL}/TRX-000123")
    assert response.status_code == 200
    assert response.json() == detail()


async def test_get_accepted_record_matches_spec_example(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory)
    await accept(session_factory, "TRX-000123", responded_at=RESPONDED_AT)

    response = await api_client.get(f"{URL}/TRX-000123")
    assert response.json() == detail(
        status="ACEPTADO",
        personalDataConsent=True,
        marketingConsentYMDP=False,
        marketingConsentDEALER=True,
        consentDateTime="2026-11-05T10:20:11-05:00",
    )


async def test_get_rejected_record_has_false_marketing(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory)
    await reject(session_factory, "TRX-000123")

    body = (await api_client.get(f"{URL}/TRX-000123")).json()
    assert body["status"] == "NO_ACEPTADO"
    assert body["isDeleted"] == 1
    assert (body["personalDataConsent"], body["marketingConsentYMDP"], body["marketingConsentDEALER"]) == (
        False,
        False,
        False,
    )
    assert body["consentDateTime"] is not None
    # customerId, dealerCode y transacción se conservan.
    assert (body["IdentifierId"], body["customerId"], body["dealerCode"]) == (
        "TRX-000123",
        "12345678",
        "DEALER-001",
    )


async def test_deleted_record_hides_marketing_even_if_stored(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    # Datos inconsistentes a propósito: la respuesta igual muestra false en el marketing de un registro borrado.
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
    assert (body["marketingConsentYMDP"], body["marketingConsentDEALER"]) == (False, False)


async def test_get_has_no_name_phone_or_email(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory, additionalInfo={"canal": "tienda"})
    response = await api_client.get(f"{URL}/TRX-000123")
    assert set(response.json()) == set(detail())
    assert_no_personal_data(response)
    assert "tienda" not in response.text


async def test_get_unknown_transaction_returns_404(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(f"{URL}/TRX-NO-EXISTE")
    assert response.status_code == 404
    assert response.json() == {"detail": "No existe un registro con ese IdentifierId"}


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
    response = await api_client.post(
        URL,
        json={"_request": {**BODY, "IdentifierId": "TRX-000999", "requestDateTime": "2026-11-06T08:00:00Z"}},
    )
    assert response.json()["affectedIdentifierId"] == "TRX-000123"
    await update_consent(session_factory, "TRX-000999", created_at=datetime(2026, 11, 6, tzinfo=UTC))

    # KIZUNA puede consultar con su propio ID: el registro tiene las respuestas que el cliente ya dio.
    response = await api_client.get(f"{URL}/TRX-000999")
    assert response.status_code == 200
    assert response.json() == detail(
        IdentifierId="TRX-000999",
        status="ACEPTADO",
        personalDataConsent=True,
        marketingConsentYMDP=False,
        marketingConsentDEALER=True,
        consentDateTime="2026-11-05T10:20:11-05:00",
        requestDateTime="2026-11-06T03:00:00-05:00",
        createdAt="2026-11-05T19:00:00-05:00",
    )


async def test_list_includes_linked_copies(api_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    await create(api_client, session_factory)
    await api_client.post(URL, json={"_request": {**BODY, "IdentifierId": "TRX-000999"}})
    await update_consent(session_factory, "TRX-000999", created_at=datetime(2026, 11, 6, tzinfo=UTC))

    body = (await search(api_client, customerId="12345678", dealerCode="DEALER-001")).json()
    assert [(r["IdentifierId"], r["status"]) for r in body] == [
        ("TRX-000999", "PENDIENTE"),
        ("TRX-000123", "PENDIENTE"),
    ]


# --- GET /api/v1/consents?customerId=...&dealerCode=... ---


async def seed_history(client: httpx.AsyncClient, sessions: Sessions) -> None:
    """Mismo cliente: dos compras con DEALER-001 (la primera rechazada) y una con DEALER-002."""
    await create(client, sessions, IdentifierId="TRX-1", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    await reject(sessions, "TRX-1")
    await create(client, sessions, IdentifierId="TRX-2", created_at=datetime(2026, 3, 1, tzinfo=UTC))
    await create(
        client,
        sessions,
        IdentifierId="TRX-3",
        dealerCode="DEALER-002",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    # Otro cliente: no debe aparecer.
    await create(client, sessions, IdentifierId="TRX-OTRO", customerId="87654321")


async def search(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    return await client.get(URL, params=params)


async def test_list_by_customer_includes_all_dealers_newest_first(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)

    response = await search(api_client, customerId="12345678")
    assert response.status_code == 200
    body = response.json()
    assert [r["IdentifierId"] for r in body] == ["TRX-2", "TRX-3", "TRX-1"]
    assert [r["createdAt"] for r in body] == [
        "2026-02-28T19:00:00-05:00",
        "2026-01-31T19:00:00-05:00",
        "2025-12-31T19:00:00-05:00",
    ]


async def test_list_filtered_by_dealer_includes_deleted_records(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)

    body = (await search(api_client, customerId="12345678", dealerCode="DEALER-001")).json()
    assert [(r["IdentifierId"], r["isDeleted"], r["status"]) for r in body] == [
        ("TRX-2", 0, "PENDIENTE"),
        ("TRX-1", 1, "NO_ACEPTADO"),
    ]
    rejected = body[1]
    assert (
        rejected["personalDataConsent"],
        rejected["marketingConsentYMDP"],
        rejected["marketingConsentDEALER"],
    ) == (False, False, False)


async def test_list_normalizes_the_query_like_the_registration(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)
    body = (await search(api_client, customerId="1234-5678", dealerCode=" DEALER-002 ")).json()
    assert [r["IdentifierId"] for r in body] == ["TRX-3"]


async def test_list_ties_on_created_at_are_ordered_by_transaction_id(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(api_client, session_factory, IdentifierId="TRX-A")
    await create(api_client, session_factory, IdentifierId="TRX-B", dealerCode="DEALER-002")
    body = (await search(api_client, customerId="12345678")).json()
    assert [r["IdentifierId"] for r in body] == ["TRX-B", "TRX-A"]


async def test_list_without_results_returns_empty_list(api_client: httpx.AsyncClient) -> None:
    response = await search(api_client, customerId="11111111")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_has_no_name_phone_or_email(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await seed_history(api_client, session_factory)
    response = await search(api_client, customerId="12345678")
    assert len(response.json()) == 3
    assert_no_personal_data(response)


@pytest.mark.parametrize(
    ("params", "error"),
    [
        (
            {},
            {"type": "missing", "loc": ["query", "customerId"], "msg": "Campo obligatorio"},
        ),
        (
            {"customerId": "12.345"},
            {
                "type": "value_error",
                "loc": ["query", "customerId"],
                "msg": "El identificador del cliente debe ser alfanumérico (letras, Ñ y dígitos), de máximo 20 "
                "caracteres",
            },
        ),
        (
            {"customerId": "12345678", "dealerCode": "  "},
            {
                "type": "value_error",
                "loc": ["query", "dealerCode"],
                "msg": "El código del dealer no puede estar vacío",
            },
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
    [(f"{URL}/TRX-000123", {}), (URL, {"customerId": "12345678"})],
)
@pytest.mark.parametrize(
    ("key", "detail_text"), [("", "Falta la API Key"), ("otra-clave", "API Key inválida")]
)
async def test_queries_require_api_key(
    api_client: httpx.AsyncClient, path: str, params: dict[str, str], key: str, detail_text: str
) -> None:
    response = await api_client.get(path, params=params, headers={"X-API-Key": key})
    assert response.status_code == 401
    assert response.json() == {"detail": detail_text}


async def test_queries_bypass_api_key_when_disabled(
    api_client: httpx.AsyncClient, session_factory: Sessions, test_settings: Settings
) -> None:
    await create(api_client, session_factory)
    test_settings.api_key_enabled = False
    headers = {"X-API-Key": ""}
    assert (await api_client.get(f"{URL}/TRX-000123", headers=headers)).status_code == 200
    response = await api_client.get(URL, params={"customerId": "12345678"}, headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_queries_are_documented() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    detail_op = paths[f"{URL}/{{IdentifierId}}"]["get"]
    listing = paths[URL]["get"]
    assert detail_op["security"] == listing["security"] == [{"ApiKey": []}]
    assert "404" in detail_op["responses"]
    params = {p["name"]: p["required"] for p in listing["parameters"]}
    assert params == {"customerId": True, "dealerCode": False}


def test_consent_model_fields_not_exposed_are_personal_data() -> None:
    # Si se agrega una columna a Consent, este test obliga a decidir si se expone en las consultas.
    exposed = {"transaction_id", "customer_id", "dealer_code", "dealer_name", "status", "responded_at"}
    exposed |= {
        "is_deleted",
        "created_at",
        "requested_at",
        "consent_personal_data",
        "consent_marketing_yamaha",
    }
    exposed |= {"consent_marketing_dealer"}
    # source_transaction_id no es dato personal, pero la respuesta de la sección 2.4 no lo incluye.
    hidden = {
        "full_name",
        "phone",
        "email",
        "additional_info",
        "deleted_at",
        "updated_at",
        "source_transaction_id",
    }
    assert set(Consent.__table__.columns.keys()) == exposed | hidden
