"""Dealers del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, sección 6.6; fase B5): criterios B5.1–B5.7 y B5.11.

Mismas reglas que usuarios (decisiones de la sesión 3): `version` sube solo con PUT; un PUT sin cambios no deja
bitácora. El dealerCode vacío usa el texto del Anexo A.3 (con punto), sin tocar el de v1.4.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.models import AuditAction, RoleCode
from app.services.backoffice.dealers import get_active_dealer_by_code
from tests.bo_helpers import (
    BO,
    BoUser,
    Sessions,
    audit_rows,
    create_bo_user,
    create_dealer_row,
    get_dealer,
    logged_client,
    make_ruc,
    update_dealer_row,
    update_user,
)

DEALERS = f"{BO}/dealers"
ISO_LIMA = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-05:00$")
ClientFactory = Callable[[], httpx.AsyncClient]

NEW_DEALER = {
    "dealerCode": "CL0001",
    "businessName": "ABC Motors S.A.C.",
    "ruc": "20100047218",
    "address": "Av. Ejemplo 123",
    "districtUbigeo": "150101",
    "contactEmail": "contacto@abcmotors.pe",
    "contactPhone": "(01) 456-7890",
    "contactName": "María López",
}
NOT_FOUND = {"detail": "No existe el dealer.", "code": "NOT_FOUND"}
DUPLICATE = {"detail": "Ya existe un dealer con ese código.", "code": "DUPLICATE", "field": "dealerCode"}
DISTRICT_NOT_FOUND = {
    "detail": "El distrito seleccionado no existe.",
    "code": "DISTRICT_NOT_FOUND",
    "field": "districtUbigeo",
}
LIMA = {
    "district": {"code": "150101", "name": "LIMA"},
    "province": {"code": "1501", "name": "LIMA"},
    "department": {"code": "15", "name": "LIMA"},
}


def ref(user: BoUser) -> dict[str, str]:
    return {
        "id": str(user.id),
        "username": user.username,
        "fullName": f"{user.first_names} {user.last_names}",
    }


@pytest.fixture
async def operator_client(make_bo_client: ClientFactory, operator_user: BoUser) -> httpx.AsyncClient:
    return await logged_client(make_bo_client, operator_user)


@pytest.fixture(params=["ADMIN", "OPERADOR"])
async def dealer_actor(
    request: pytest.FixtureRequest, make_bo_client: ClientFactory, admin_user: BoUser, operator_user: BoUser
) -> tuple[httpx.AsyncClient, BoUser]:
    """B5.11: el Operador tiene acceso completo a Dealers, igual que el Administrador."""
    user = admin_user if request.param == "ADMIN" else operator_user
    return await logged_client(make_bo_client, user), user


async def create(client: httpx.AsyncClient, **changes: Any) -> dict[str, Any]:
    response = await client.post(DEALERS, json={**NEW_DEALER, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def put_body(detail: dict[str, Any], **changes: Any) -> dict[str, Any]:
    body = {
        "businessName": detail["businessName"],
        "ruc": detail["ruc"],
        "address": detail["address"],
        "districtUbigeo": detail["district"]["code"],
        "contactEmail": detail["contactEmail"],
        "contactPhone": detail["contactPhone"],
        "contactName": detail["contactName"],
        "version": detail["version"],
    }
    body.update(changes)
    return body


# --- Acceso (B5.11) ---

DEALER_ENDPOINTS = [
    ("GET", "/dealers"),
    ("POST", "/dealers"),
    ("GET", "/dealers/lookup?dealerCode=CL0001"),
    ("GET", "/dealers/export"),
    ("GET", "/dealers/{id}"),
    ("PUT", "/dealers/{id}"),
    ("DELETE", "/dealers/{id}"),
    ("GET", "/ubigeo/departments"),
    ("GET", "/ubigeo/departments/15/provinces"),
    ("GET", "/ubigeo/provinces/1501/districts"),
]


@pytest.mark.parametrize(("method", "path"), DEALER_ENDPOINTS)
async def test_dealer_endpoints_require_a_session(
    bo_client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await bo_client.request(method, f"{BO}{path.format(id=uuid.uuid4())}")
    assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"


async def test_pending_password_change_blocks_dealers(
    make_bo_client: ClientFactory, session_factory: Sessions
) -> None:
    user = await create_bo_user(
        session_factory,
        username="nuevo.qa",
        dni="40000001",
        email="nuevo.qa@correo.test",
        must_change_password=True,
    )
    client = await logged_client(make_bo_client, user)
    response = await client.get(DEALERS)
    assert response.status_code == 403 and response.json()["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_full_crud_for_both_roles(
    dealer_actor: tuple[httpx.AsyncClient, BoUser], session_factory: Sessions
) -> None:
    client, user = dealer_actor
    created = await create(client)
    dealer_id = created["id"]

    assert (await client.get(f"{DEALERS}/{dealer_id}")).status_code == 200
    assert (await client.get(DEALERS)).json()["total"] == 1
    assert (await client.get(f"{DEALERS}/lookup", params={"dealerCode": "cl0001"})).status_code == 200
    updated = await client.put(f"{DEALERS}/{dealer_id}", json=put_body(created, address="Av. Nueva 456"))
    assert updated.status_code == 200 and updated.json()["updatedBy"] == ref(user)
    export = await client.get(f"{DEALERS}/export")
    assert export.status_code == 200 and export.content[:2] == b"PK"
    for path in (
        "/ubigeo/departments",
        "/ubigeo/departments/15/provinces",
        "/ubigeo/provinces/1501/districts",
    ):
        assert (await client.get(f"{BO}{path}")).status_code == 200, path
    assert (await client.delete(f"{DEALERS}/{dealer_id}")).status_code == 204

    actions = [row.action for row in await audit_rows(session_factory) if row.action.startswith("DEALER")]
    assert sorted(actions) == ["DEALERS_EXPORTED", "DEALER_CREATED", "DEALER_DELETED", "DEALER_UPDATED"]
    assert all(
        row.actor_user_id == user.id
        for row in await audit_rows(session_factory)
        if row.action.startswith("DEALER")
    )


# --- B5.1 CRUD con bitácora ---


async def test_create_dealer(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    response = await operator_client.post(DEALERS, json=NEW_DEALER)

    assert response.status_code == 201, response.text
    body = response.json()
    dealer_id = body.pop("id")
    for key in ("createdAt", "updatedAt"):
        assert ISO_LIMA.match(body.pop(key))
    assert body == {
        "dealerCode": "CL0001",
        "businessName": "ABC Motors S.A.C.",
        "ruc": "20100047218",
        "address": "Av. Ejemplo 123",
        **LIMA,
        "contactEmail": "contacto@abcmotors.pe",
        "contactPhone": "014567890",
        "contactName": "María López",
        "createdBy": ref(operator_user),
        "updatedBy": ref(operator_user),
        "version": 1,
    }

    stored = await get_dealer(session_factory, uuid.UUID(dealer_id))
    assert stored.created_by == operator_user.id and stored.is_deleted == 0
    (entry,) = await audit_rows(session_factory, AuditAction.DEALER_CREATED)
    assert entry.actor_user_id == operator_user.id
    assert entry.entity_type == "DEALER" and entry.entity_id == dealer_id
    assert entry.details == {
        "dealerCode": "CL0001",
        "businessName": "ABC Motors S.A.C.",
        "ruc": "20100047218",
        "address": "Av. Ejemplo 123",
        "districtUbigeo": "150101",
        "contactEmail": "contacto@abcmotors.pe",
        "contactPhone": "014567890",
        "contactName": "María López",
    }


async def test_create_dealer_normalizes_values(operator_client: httpx.AsyncClient) -> None:
    body = await create(
        operator_client,
        dealerCode="  cl-0001 A ",
        businessName="  ABC   Motors  ",
        ruc=" 201 0004 7218 ",
        address="  Av.   Ejemplo   123 ",
        districtUbigeo=" 150501 ",
        contactEmail="  Contacto@ABCMotors.PE ",
        contactPhone="+51 1 456.7890",
        contactName="  maría   lópez ",
    )
    assert body["dealerCode"] == "cl-0001 A"  # solo trim (como consents.dealer_code)
    assert (body["businessName"], body["ruc"], body["address"]) == (
        "ABC Motors",
        "20100047218",
        "Av. Ejemplo 123",
    )
    assert body["contactEmail"] == "contacto@abcmotors.pe"
    assert (body["contactPhone"], body["contactName"]) == ("+5114567890", "maría lópez")
    assert body["district"] == {"code": "150501", "name": "SAN VICENTE DE CAÑETE"}
    assert body["province"] == {"code": "1505", "name": "CAÑETE"}


@pytest.mark.parametrize("empty", [None, "", "   "])
async def test_optional_fields_empty_become_null(
    operator_client: httpx.AsyncClient, empty: str | None
) -> None:
    body = await create(operator_client, businessName=empty, contactPhone=empty, contactName=empty)
    assert (body["businessName"], body["contactPhone"], body["contactName"]) == (None, None, None)


async def test_optional_fields_can_be_omitted(operator_client: httpx.AsyncClient) -> None:
    required = {
        key: NEW_DEALER[key] for key in ("dealerCode", "ruc", "address", "districtUbigeo", "contactEmail")
    }
    response = await operator_client.post(DEALERS, json=required)
    assert response.status_code == 201
    assert response.json()["businessName"] is None


async def test_get_dealer_not_found(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    response = await operator_client.get(f"{DEALERS}/{uuid.uuid4()}")
    assert response.status_code == 404 and response.json() == NOT_FOUND

    dealer_id = await create_dealer_row(session_factory, dealer_code="CL0009", is_deleted=1)
    response = await operator_client.get(f"{DEALERS}/{dealer_id}")
    assert response.status_code == 404 and response.json() == NOT_FOUND

    response = await operator_client.get(f"{DEALERS}/no-es-uuid")
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["path", "dealerId"]


async def test_update_dealer(
    operator_client: httpx.AsyncClient,
    make_bo_client: ClientFactory,
    operator_user: BoUser,
    admin_user: BoUser,
    session_factory: Sessions,
) -> None:
    created = await create(await logged_client(make_bo_client, admin_user))

    response = await operator_client.put(
        f"{DEALERS}/{created['id']}",
        json=put_body(created, address="Jr. Nuevo 456", districtUbigeo="070101", contactPhone=None),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["address"] == "Jr. Nuevo 456" and body["contactPhone"] is None
    assert body["district"] == {"code": "070101", "name": "CALLAO"}
    assert body["department"] == {"code": "07", "name": "CALLAO"}
    assert body["version"] == 2
    assert body["createdBy"] == ref(admin_user) and body["updatedBy"] == ref(operator_user)
    assert body["dealerCode"] == "CL0001"

    (entry,) = await audit_rows(session_factory, AuditAction.DEALER_UPDATED)
    assert entry.actor_user_id == operator_user.id
    assert entry.details == {
        "dealerCode": "CL0001",
        "changes": {
            "address": {"from": "Av. Ejemplo 123", "to": "Jr. Nuevo 456"},
            "districtUbigeo": {"from": "150101", "to": "070101"},
            "contactPhone": {"from": "014567890", "to": None},
        },
    }


async def test_put_replaces_optional_fields(operator_client: httpx.AsyncClient) -> None:
    created = await create(operator_client)
    body = put_body(created)
    for key in ("businessName", "contactPhone", "contactName"):
        del body[key]
    response = await operator_client.put(f"{DEALERS}/{created['id']}", json=body)
    assert response.status_code == 200
    assert (
        response.json()["businessName"],
        response.json()["contactPhone"],
        response.json()["contactName"],
    ) == (
        None,
        None,
        None,
    )


async def test_put_without_changes(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    created = await create(operator_client)
    before = await get_dealer(session_factory, uuid.UUID(created["id"]))
    response = await operator_client.put(
        f"{DEALERS}/{created['id']}", json=put_body(created, contactEmail="CONTACTO@abcmotors.pe")
    )
    assert response.status_code == 200 and response.json()["version"] == 1
    after = await get_dealer(session_factory, uuid.UUID(created["id"]))
    assert after.updated_at == before.updated_at
    assert await audit_rows(session_factory, AuditAction.DEALER_UPDATED) == []


async def test_dealer_code_is_not_editable(operator_client: httpx.AsyncClient) -> None:
    created = await create(operator_client)
    response = await operator_client.put(
        f"{DEALERS}/{created['id']}", json=put_body(created, dealerCode="CL0002")
    )
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "extra_forbidden", "loc": ["body", "dealerCode"], "msg": "Campo no permitido"}
    ]


async def test_update_unknown_or_deleted_dealer(operator_client: httpx.AsyncClient) -> None:
    created = await create(operator_client)
    response = await operator_client.put(f"{DEALERS}/{uuid.uuid4()}", json=put_body(created))
    assert response.status_code == 404 and response.json() == NOT_FOUND
    assert (await operator_client.delete(f"{DEALERS}/{created['id']}")).status_code == 204
    response = await operator_client.put(f"{DEALERS}/{created['id']}", json=put_body(created))
    assert response.status_code == 404


async def test_delete_dealer(
    operator_client: httpx.AsyncClient, operator_user: BoUser, session_factory: Sessions
) -> None:
    created = await create(operator_client)

    response = await operator_client.delete(f"{DEALERS}/{created['id']}")

    assert response.status_code == 204 and response.content == b""
    stored = await get_dealer(session_factory, uuid.UUID(created["id"]))
    assert stored.is_deleted == 1 and stored.deleted_at is not None
    assert stored.deleted_by == operator_user.id and stored.updated_by == operator_user.id
    assert (await operator_client.get(f"{DEALERS}/{created['id']}")).status_code == 404
    assert (await operator_client.get(DEALERS)).json()["total"] == 0
    assert (await operator_client.delete(f"{DEALERS}/{created['id']}")).status_code == 404

    (entry,) = await audit_rows(session_factory, AuditAction.DEALER_DELETED)
    assert entry.actor_user_id == operator_user.id and entry.entity_id == created["id"]
    assert entry.details == {"dealerCode": "CL0001"}


# --- Validaciones (B5.1, B5.2) ---


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dealerCode", "", "El código del dealer no puede estar vacío."),
        ("dealerCode", "   ", "El código del dealer no puede estar vacío."),
        ("dealerCode", "C" * 101, "El código del dealer no puede tener más de 100 caracteres"),
        ("businessName", "B" * 251, "Debe tener como máximo 250 caracteres"),
        ("ruc", "20100047219", "El RUC no es válido."),
        ("ruc", "30100047218", "El RUC no es válido."),
        ("ruc", "2010004721", "El RUC debe tener 11 dígitos."),
        ("ruc", "2010004721A", "El RUC debe tener 11 dígitos."),
        ("address", "  ", "No puede estar vacío"),
        ("address", "A" * 251, "Debe tener como máximo 250 caracteres"),
        ("districtUbigeo", "", "Selecciona un distrito."),
        ("districtUbigeo", "15010", "Selecciona un distrito."),
        ("districtUbigeo", "15010A", "Selecciona un distrito."),
        ("contactEmail", "contacto@", "El correo no tiene un formato válido."),
        ("contactEmail", "", "El correo no tiene un formato válido."),
        ("contactPhone", "12345", "El teléfono debe tener entre 6 y 15 dígitos."),
        ("contactPhone", "1234567890123456", "El teléfono debe tener entre 6 y 15 dígitos."),
        ("contactPhone", "01-456-789 anexo 2", "El teléfono debe tener entre 6 y 15 dígitos."),
        ("contactPhone", "01+4567890", "El teléfono debe tener entre 6 y 15 dígitos."),
        (
            "contactName",
            "María 2",
            "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion.",
        ),
        ("contactName", "a" * 151, "Debe tener como máximo 150 caracteres"),
        ("isDeleted", 1, "Campo no permitido"),
        ("ruc", 20100047218, "Debe ser un texto"),
    ],
)
async def test_create_dealer_validation(
    operator_client: httpx.AsyncClient, session_factory: Sessions, field: str, value: Any, message: str
) -> None:
    response = await operator_client.post(DEALERS, json={**NEW_DEALER, field: value})
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", field] and error["msg"] == message
    assert "input" not in error
    assert await audit_rows(session_factory, AuditAction.DEALER_CREATED) == []


@pytest.mark.parametrize("field", ["dealerCode", "ruc", "address", "districtUbigeo", "contactEmail"])
async def test_required_fields(operator_client: httpx.AsyncClient, field: str) -> None:
    body = {key: value for key, value in NEW_DEALER.items() if key != field}
    response = await operator_client.post(DEALERS, json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "missing", "loc": ["body", field], "msg": "Campo obligatorio"}
    ]


async def test_valid_phone_and_name_limits(operator_client: httpx.AsyncClient) -> None:
    body = await create(operator_client, contactPhone="123456", contactName="a" * 150, businessName="B" * 250)
    assert body["contactPhone"] == "123456"
    body = await create(operator_client, dealerCode="CL0002", contactPhone="+123456789012345")
    assert body["contactPhone"] == "+123456789012345"


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"version": 0}, "version", "Debe ser mayor o igual que 1"),
        ({"ruc": "20100047219"}, "ruc", "El RUC no es válido."),
        ({"districtUbigeo": "1501"}, "districtUbigeo", "Selecciona un distrito."),
    ],
)
async def test_update_dealer_validation(
    operator_client: httpx.AsyncClient, changes: dict[str, Any], field: str, message: str
) -> None:
    created = await create(operator_client)
    response = await operator_client.put(f"{DEALERS}/{created['id']}", json=put_body(created, **changes))
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", field] and error["msg"] == message


# --- B5.3 Código duplicado ---


async def test_duplicate_code_ignoring_case(
    operator_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create(operator_client)
    for code in ("CL0001", "cl0001", "  Cl0001 "):
        response = await operator_client.post(DEALERS, json={**NEW_DEALER, "dealerCode": code})
        assert response.status_code == 409 and response.json() == DUPLICATE, code
    assert len(await audit_rows(session_factory, AuditAction.DEALER_CREATED)) == 1


async def test_concurrent_creations_only_one_wins(
    make_bo_client: ClientFactory, operator_user: BoUser, session_factory: Sessions
) -> None:
    first = await logged_client(make_bo_client, operator_user)
    second = await logged_client(make_bo_client, operator_user)
    responses = await asyncio.gather(
        first.post(DEALERS, json={**NEW_DEALER, "dealerCode": "CL0002"}),
        second.post(DEALERS, json={**NEW_DEALER, "dealerCode": "cl0002"}),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    (conflict,) = [response for response in responses if response.status_code == 409]
    assert conflict.json() == DUPLICATE
    assert len(await audit_rows(session_factory, AuditAction.DEALER_CREATED)) == 1


async def test_code_of_deleted_dealer_can_be_reused(operator_client: httpx.AsyncClient) -> None:
    first = await create(operator_client)
    assert (await operator_client.delete(f"{DEALERS}/{first['id']}")).status_code == 204
    second = await create(operator_client, dealerCode="cl0001")
    assert second["id"] != first["id"] and second["dealerCode"] == "cl0001"


# --- B5.4 Distrito inexistente ---


async def test_district_not_found(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    response = await operator_client.post(DEALERS, json={**NEW_DEALER, "districtUbigeo": "999999"})
    assert response.status_code == 422 and response.json() == DISTRICT_NOT_FOUND

    created = await create(operator_client)
    response = await operator_client.put(
        f"{DEALERS}/{created['id']}", json=put_body(created, districtUbigeo="150199")
    )
    assert response.status_code == 422 and response.json() == DISTRICT_NOT_FOUND
    assert (await get_dealer(session_factory, uuid.UUID(created["id"]))).district_ubigeo == "150101"


# --- B5.5 Concurrencia optimista ---


async def test_version_conflict(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    stale = await create(operator_client)
    first = await operator_client.put(f"{DEALERS}/{stale['id']}", json=put_body(stale, address="Primera 1"))
    assert first.json()["version"] == 2

    response = await operator_client.put(
        f"{DEALERS}/{stale['id']}", json=put_body(stale, address="Segunda 2")
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "El registro fue modificado por otro usuario. Recarga la página e inténtalo de nuevo.",
        "code": "VERSION_CONFLICT",
    }
    stored = await get_dealer(session_factory, uuid.UUID(stale["id"]))
    assert stored.address == "Primera 1" and stored.version == 2


# --- B5.7 lookup ---


async def test_lookup(operator_client: httpx.AsyncClient) -> None:
    created = await create(operator_client)
    for code in ("cl0001", "CL0001", "  Cl0001 "):
        response = await operator_client.get(f"{DEALERS}/lookup", params={"dealerCode": code})
        assert response.status_code == 200 and response.json() == created, code

    assert (await operator_client.delete(f"{DEALERS}/{created['id']}")).status_code == 204
    response = await operator_client.get(f"{DEALERS}/lookup", params={"dealerCode": "CL0001"})
    assert response.status_code == 404 and response.json() == NOT_FOUND
    response = await operator_client.get(f"{DEALERS}/lookup", params={"dealerCode": "NO-EXISTE"})
    assert response.status_code == 404


async def test_lookup_validation(operator_client: httpx.AsyncClient) -> None:
    response = await operator_client.get(f"{DEALERS}/lookup")
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "missing", "loc": ["query", "dealerCode"], "msg": "Campo obligatorio"}
    ]
    response = await operator_client.get(f"{DEALERS}/lookup", params={"dealerCode": "  "})
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "El código del dealer no puede estar vacío."


async def test_get_active_dealer_by_code_is_reusable(session_factory: Sessions) -> None:
    """Punto de reutilización para la landing (sección 13, F1): sin distinguir mayúsculas, solo vigentes."""
    await create_dealer_row(session_factory, dealer_code="CL0001", is_deleted=1)
    active_id = await create_dealer_row(session_factory, dealer_code="cl0001")
    async with session_factory() as db:
        dealer = await get_active_dealer_by_code(db, " CL0001 ")
        assert dealer is not None and dealer.id == active_id
        assert await get_active_dealer_by_code(db, "CL0002") is None


# --- B5.6 Lista ---


@pytest.fixture
async def listed_dealers(session_factory: Sessions) -> dict[str, uuid.UUID]:
    """Los códigos son ASCII a propósito: el orden de letras con tilde o Ñ depende de la colación de la base (la
    imagen local de PostgreSQL, sobre musl, ordena por punto de código)."""
    base = datetime.now(UTC) - timedelta(days=10)
    rows = {
        "CL0001": {
            "business_name": "Motos Lima S.A.C.",
            "district_ubigeo": "150101",
            "contact_name": "Ana Pérez",
        },
        "cl0002": {"business_name": None, "district_ubigeo": "150132", "ruc": make_ruc(77)},
        "CU0001": {
            "business_name": "Cusco Motor",
            "district_ubigeo": "080101",
            "contact_name": "Luis Quispe",
        },
        "CA0001": {
            "business_name": "Callao 50% Motos",
            "district_ubigeo": "070101",
            "contact_name": "José Mora",
        },
        "CN0001": {"business_name": "Cañete_Motos", "district_ubigeo": "150501"},
        "BORRADO": {"business_name": "Motos Lima Borrado", "district_ubigeo": "150101", "is_deleted": 1},
    }
    ids = {}
    for position, (code, values) in enumerate(rows.items()):
        ids[code] = await create_dealer_row(
            session_factory,
            dealer_code=code,
            created_at=base + timedelta(hours=position),
            updated_at=base + timedelta(hours=10 - position),
            **values,
        )
    return ids


async def codes(client: httpx.AsyncClient, **params: Any) -> list[str]:
    response = await client.get(DEALERS, params=params)
    assert response.status_code == 200, response.text
    return [item["dealerCode"] for item in response.json()["items"]]


async def test_list_default_order_and_shape(
    operator_client: httpx.AsyncClient, listed_dealers: dict[str, uuid.UUID]
) -> None:
    body = (await operator_client.get(DEALERS)).json()
    assert (body["total"], body["page"], body["pageSize"]) == (5, 1, 20)
    # dealerCode sin distinguir mayúsculas; sin el eliminado.
    assert [item["dealerCode"] for item in body["items"]] == [
        "CA0001",
        "CL0001",
        "cl0002",
        "CN0001",
        "CU0001",
    ]
    item = body["items"][1]
    assert item["district"] == LIMA["district"] and item["createdBy"] is None


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("cl00", ["CL0001", "cl0002"]),
        ("motos lima", ["CL0001"]),
        ("QUISPE", ["CU0001"]),
        (make_ruc(77)[:6], ["cl0002"]),
        # Comodines literales: sin escapar, "0%" coincidiría con todos y "e_m" también con "José Mora".
        ("0%", ["CA0001"]),
        ("e_m", ["CN0001"]),
        ("borrado", []),
    ],
)
async def test_list_search(
    operator_client: httpx.AsyncClient, listed_dealers: dict[str, uuid.UUID], q: str, expected: list[str]
) -> None:
    assert await codes(operator_client, q=q) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"departmentCode": "15"}, ["CL0001", "cl0002", "CN0001"]),
        ({"provinceCode": "1501"}, ["CL0001", "cl0002"]),
        ({"provinceCode": "1505"}, ["CN0001"]),
        ({"districtUbigeo": "150132"}, ["cl0002"]),
        ({"departmentCode": "07"}, ["CA0001"]),
        ({"departmentCode": "15", "q": "motos"}, ["CL0001", "CN0001"]),
        # Combinación inconsistente o código inexistente: sin resultados.
        ({"departmentCode": "07", "provinceCode": "1501"}, []),
        ({"departmentCode": "99"}, []),
        ({"departmentCode": ""}, ["CA0001", "CL0001", "cl0002", "CN0001", "CU0001"]),
    ],
)
async def test_list_ubigeo_filters(
    operator_client: httpx.AsyncClient,
    listed_dealers: dict[str, uuid.UUID],
    params: dict[str, Any],
    expected: list[str],
) -> None:
    assert await codes(operator_client, **params) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"sortOrder": "desc"}, ["CU0001", "CN0001", "cl0002", "CL0001", "CA0001"]),
        # Sin razón social: al final en ambos sentidos.
        ({"sortBy": "businessName"}, ["CA0001", "CN0001", "CU0001", "CL0001", "cl0002"]),
        ({"sortBy": "businessName", "sortOrder": "desc"}, ["CL0001", "CU0001", "CN0001", "CA0001", "cl0002"]),
        ({"sortBy": "createdAt"}, ["CL0001", "cl0002", "CU0001", "CA0001", "CN0001"]),
        ({"sortBy": "updatedAt"}, ["CN0001", "CA0001", "CU0001", "cl0002", "CL0001"]),
        ({"sortBy": "createdAt", "sortOrder": "desc"}, ["CN0001", "CA0001", "CU0001", "cl0002", "CL0001"]),
    ],
)
async def test_list_sorting(
    operator_client: httpx.AsyncClient,
    listed_dealers: dict[str, uuid.UUID],
    params: dict[str, Any],
    expected: list[str],
) -> None:
    assert await codes(operator_client, **params) == expected


async def test_list_sort_by_ruc(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    for number, code in ((30, "A"), (10, "B"), (20, "C")):
        await create_dealer_row(session_factory, dealer_code=code, ruc=make_ruc(number))
    assert await codes(operator_client, sortBy="ruc") == ["B", "C", "A"]
    assert await codes(operator_client, sortBy="ruc", sortOrder="desc") == ["A", "C", "B"]


async def test_list_pagination(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    for number in range(12):
        await create_dealer_row(session_factory, dealer_code=f"D{number:02d}")
    page_one = (await operator_client.get(DEALERS, params={"pageSize": 10})).json()
    page_two = (await operator_client.get(DEALERS, params={"pageSize": 10, "page": 2})).json()
    assert (page_one["total"], len(page_one["items"]), len(page_two["items"])) == (12, 10, 2)
    assert [item["dealerCode"] for item in page_two["items"]] == ["D10", "D11"]


@pytest.mark.parametrize(
    ("params", "field", "message"),
    [
        ({"departmentCode": "1"}, "departmentCode", "Debe tener 2 dígitos"),
        ({"provinceCode": "15A1"}, "provinceCode", "Debe tener 4 dígitos"),
        ({"districtUbigeo": "15010"}, "districtUbigeo", "Debe tener 6 dígitos"),
        ({"sortBy": "address"}, "sortBy", None),
        ({"pageSize": 25}, "pageSize", "Debe ser uno de estos valores: 10, 20, 50 o 100"),
        ({"q": "x" * 101}, "q", "Debe tener como máximo 100 caracteres"),
    ],
)
async def test_list_query_validation(
    operator_client: httpx.AsyncClient, params: dict[str, Any], field: str, message: str | None
) -> None:
    response = await operator_client.get(DEALERS, params=params)
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["query", field]
    if message is not None:
        assert error["msg"] == message


async def test_detail_shows_authors_even_if_deleted(
    operator_client: httpx.AsyncClient, session_factory: Sessions, operator_user: BoUser
) -> None:
    author = await create_bo_user(
        session_factory,
        username="borrado.qa",
        dni="40000002",
        email="borrado.qa@correo.test",
        role_code=RoleCode.ADMIN,
    )
    dealer_id = await create_dealer_row(session_factory, dealer_code="CL0001", created_by=author.id)
    await update_user(session_factory, author.id, is_deleted=1)
    detail = (await operator_client.get(f"{DEALERS}/{dealer_id}")).json()
    assert detail["createdBy"] == ref(author) and detail["updatedBy"] is None
    await update_dealer_row(session_factory, dealer_id, updated_by=operator_user.id)
    assert (await operator_client.get(f"{DEALERS}/{dealer_id}")).json()["updatedBy"] == ref(operator_user)


# --- Nada sensible en los logs ---


async def test_dealer_logs_have_no_personal_data(
    operator_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        created = await create(operator_client)
        await operator_client.put(
            f"{DEALERS}/{created['id']}", json=put_body(created, contactName="Rosa Ríos")
        )
        await operator_client.post(DEALERS, json=NEW_DEALER)  # 409
        await operator_client.post(DEALERS, json={**NEW_DEALER, "ruc": "20100047219"})  # 422
        await operator_client.get(DEALERS, params={"q": "20100047218"})
        await operator_client.get(f"{DEALERS}/export")
        await operator_client.delete(f"{DEALERS}/{created['id']}")

    records = [record for record in caplog.records if record.name != "httpx"]
    assert any(getattr(record, "dealer_code", None) == "CL0001" for record in records)
    text = json.dumps([vars(record) for record in records], default=str)
    for secret in ("20100047218", "abcmotors", "014567890", "María López", "Rosa Ríos", "ABC Motors"):
        assert secret not in text, secret
