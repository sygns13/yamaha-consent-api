"""Bitácora de auditoría (BACKOFFICE_DEALERS.spec.md v1.2, sección 6.8; fase B5): criterio B5.10.

Las fechas `from`/`to` se interpretan en hora de Lima (UTC−5): el día 10/11 en Lima va de 2026-11-10T05:00Z a
2026-11-11T04:59:59Z.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.models import AuditAction, AuditEntityType
from app.services.backoffice.audit import AuditContext, record_audit
from tests.bo_helpers import BO, BoUser, Sessions, logged_client, update_user

AUDIT = f"{BO}/audit-logs"
ClientFactory = Callable[[], httpx.AsyncClient]
DEALER_ID = str(uuid.uuid4())


@pytest.fixture
async def admin_client(make_bo_client: ClientFactory, admin_user: BoUser) -> httpx.AsyncClient:
    return await logged_client(make_bo_client, admin_user)


async def insert_audit(
    sessions: Sessions, action: AuditAction, occurred_at: datetime, **values: Any
) -> uuid.UUID:
    async with sessions.begin() as session:
        entry = record_audit(
            session,
            action,
            context=values.pop("context", AuditContext(ip_address="203.0.113.7", user_agent="Navegador")),
            occurred_at=occurred_at,
            **values,
        )
        await session.flush()
        return entry.id


@pytest.fixture
async def entries(
    session_factory: Sessions, admin_user: BoUser, operator_user: BoUser
) -> dict[str, uuid.UUID]:
    """Cuatro filas alrededor del 10/11/2026 en hora de Lima (además del login de admin_client)."""
    return {
        # 09/11 23:59:59 en Lima.
        "before": await insert_audit(
            session_factory,
            AuditAction.AUTH_LOGIN_FAILED,
            datetime(2026, 11, 10, 4, 59, 59, tzinfo=UTC),
            entity_type=AuditEntityType.USER,
            details={"reason": "UNKNOWN_USER"},
            context=AuditContext(),
        ),
        # 10/11 00:00:00 en Lima.
        "start": await insert_audit(
            session_factory,
            AuditAction.DEALER_CREATED,
            datetime(2026, 11, 10, 5, 0, 0, tzinfo=UTC),
            actor_user_id=operator_user.id,
            entity_type=AuditEntityType.DEALER,
            entity_id=DEALER_ID,
            details={"dealerCode": "CL0001"},
        ),
        # 10/11 23:59:59 en Lima.
        "end": await insert_audit(
            session_factory,
            AuditAction.USER_UPDATED,
            datetime(2026, 11, 11, 4, 59, 59, tzinfo=UTC),
            actor_user_id=admin_user.id,
            entity_type=AuditEntityType.USER,
            entity_id=operator_user.id,
            details={"changes": {"phone": {"from": None, "to": "912345678"}}},
        ),
        # 11/11 00:00:00 en Lima.
        "after": await insert_audit(
            session_factory,
            AuditAction.DEALER_DELETED,
            datetime(2026, 11, 11, 5, 0, 0, tzinfo=UTC),
            actor_user_id=operator_user.id,
            entity_type=AuditEntityType.DEALER,
            entity_id=DEALER_ID,
            details={"dealerCode": "CL0001"},
        ),
    }


async def ids(client: httpx.AsyncClient, **params: Any) -> list[str]:
    response = await client.get(AUDIT, params=params)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def expected(entries: dict[str, uuid.UUID], *keys: str) -> list[str]:
    return [str(entries[key]) for key in keys]


async def test_audit_log_items_and_order(
    admin_client: httpx.AsyncClient,
    admin_user: BoUser,
    operator_user: BoUser,
    entries: dict[str, uuid.UUID],
) -> None:
    body = (await admin_client.get(AUDIT)).json()
    # El login de admin_client (hoy) va primero: orden fijo por occurredAt descendente.
    assert (body["total"], body["page"], body["pageSize"]) == (5, 1, 20)
    assert body["items"][0]["action"] == "AUTH_LOGIN_SUCCEEDED"
    assert [item["id"] for item in body["items"][1:]] == expected(entries, "after", "end", "start", "before")

    end = body["items"][2]
    assert end == {
        "id": str(entries["end"]),
        "occurredAt": "2026-11-10T23:59:59-05:00",
        "actor": {"id": str(admin_user.id), "username": admin_user.username, "fullName": "Juan Pérez Gómez"},
        "action": "USER_UPDATED",
        "entityType": "USER",
        "entityId": str(operator_user.id),
        "details": {"changes": {"phone": {"from": None, "to": "912345678"}}},
        "ipAddress": "203.0.113.7",
        "userAgent": "Navegador",
    }
    before = body["items"][4]
    assert before["actor"] is None and before["entityId"] is None
    assert before["ipAddress"] is None and before["userAgent"] is None


@pytest.mark.parametrize(
    ("params", "keys"),
    [
        ({"from": "2026-11-10", "to": "2026-11-10"}, ("end", "start")),
        ({"from": "2026-11-10", "to": "2026-11-11"}, ("after", "end", "start")),
        ({"to": "2026-11-10"}, ("end", "start", "before")),
        ({"to": "2026-11-09"}, ("before",)),
        ({"from": "2026-11-11", "to": "2026-11-11"}, ("after",)),
        ({"from": "2026-11-09", "to": "2026-11-09"}, ("before",)),
    ],
)
async def test_date_filters_in_lima_time(
    admin_client: httpx.AsyncClient,
    entries: dict[str, uuid.UUID],
    params: dict[str, str],
    keys: tuple[str, ...],
) -> None:
    assert await ids(admin_client, **params) == expected(entries, *keys)


async def test_from_includes_today(admin_client: httpx.AsyncClient, entries: dict[str, uuid.UUID]) -> None:
    # Solo el login de hoy y la fila del 11/11 son desde el 11/11 en adelante.
    response = (await admin_client.get(AUDIT, params={"from": "2026-11-11"})).json()
    assert [item["action"] for item in response["items"]] == ["AUTH_LOGIN_SUCCEEDED", "DEALER_DELETED"]


async def test_other_filters(
    admin_client: httpx.AsyncClient, operator_user: BoUser, entries: dict[str, uuid.UUID]
) -> None:
    assert await ids(admin_client, actorUserId=str(operator_user.id)) == expected(entries, "after", "start")
    assert await ids(admin_client, action="USER_UPDATED") == expected(entries, "end")
    assert await ids(admin_client, entityType="DEALER") == expected(entries, "after", "start")
    assert await ids(admin_client, entityId=DEALER_ID) == expected(entries, "after", "start")
    assert await ids(admin_client, entityType="DEALER", action="DEALER_DELETED") == expected(entries, "after")
    assert await ids(admin_client, actorUserId=str(uuid.uuid4())) == []
    assert await ids(
        admin_client, actorUserId=str(operator_user.id), **{"from": "2026-11-11", "to": "2026-11-11"}
    ) == expected(entries, "after")


async def test_pagination(admin_client: httpx.AsyncClient, entries: dict[str, uuid.UUID]) -> None:
    page_two = (await admin_client.get(AUDIT, params={"pageSize": 10, "page": 2})).json()
    assert (page_two["total"], page_two["items"]) == (5, [])
    for number in range(8):
        await insert_audit_now(admin_client, number)
    page_one = (await admin_client.get(AUDIT, params={"pageSize": 10})).json()
    page_two = (await admin_client.get(AUDIT, params={"pageSize": 10, "page": 2})).json()
    assert (page_one["total"], len(page_one["items"]), len(page_two["items"])) == (13, 10, 3)
    assert len({item["id"] for item in page_one["items"] + page_two["items"]}) == 13


async def insert_audit_now(client: httpx.AsyncClient, number: int) -> None:
    # Cada perfil editado deja una fila PROFILE_UPDATED.
    response = await client.patch(f"{BO}/profile", json={"phone": f"9{number:08d}"})
    assert response.status_code == 200


async def test_actor_of_a_deleted_user_is_kept(
    admin_client: httpx.AsyncClient,
    operator_user: BoUser,
    session_factory: Sessions,
    entries: dict[str, uuid.UUID],
) -> None:
    await update_user(session_factory, operator_user.id, is_deleted=1)
    items = (await admin_client.get(AUDIT, params={"action": "DEALER_CREATED"})).json()["items"]
    assert items[0]["actor"]["username"] == operator_user.username


@pytest.mark.parametrize(
    ("params", "field", "message"),
    [
        (
            {"from": "2026-11-11", "to": "2026-11-10"},
            "to",
            "La fecha final no puede ser anterior a la inicial.",
        ),
        ({"from": "2026-13-01"}, "from", "Debe ser una fecha válida (AAAA-MM-DD)"),
        ({"to": "10/11/2026"}, "to", "Debe ser una fecha válida (AAAA-MM-DD)"),
        ({"actorUserId": "no-es-uuid"}, "actorUserId", "Debe ser un UUID válido"),
        ({"action": "BORRAR_TODO"}, "action", None),
        ({"entityType": "CONSENT"}, "entityType", "Debe ser uno de estos valores: 'USER' o 'DEALER'"),
        ({"entityId": "x" * 101}, "entityId", "Debe tener como máximo 100 caracteres"),
        ({"pageSize": 5}, "pageSize", "Debe ser uno de estos valores: 10, 20, 50 o 100"),
    ],
)
async def test_query_validation(
    admin_client: httpx.AsyncClient, params: dict[str, str], field: str, message: str | None
) -> None:
    response = await admin_client.get(AUDIT, params=params)
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["query", field]
    if message is not None:
        assert error["msg"] == message


async def test_same_day_range_is_valid(admin_client: httpx.AsyncClient) -> None:
    response = await admin_client.get(AUDIT, params={"from": "2026-11-10", "to": "2026-11-10"})
    assert response.status_code == 200


async def test_operator_is_forbidden(make_bo_client: ClientFactory, operator_user: BoUser) -> None:
    client = await logged_client(make_bo_client, operator_user)
    response = await client.get(AUDIT)
    assert response.status_code == 403
    assert response.json() == {"detail": "No tienes permiso para realizar esta acción.", "code": "FORBIDDEN"}


async def test_audit_logs_require_a_session(bo_client: httpx.AsyncClient) -> None:
    response = await bo_client.get(AUDIT)
    assert response.status_code == 401 and response.json()["code"] == "AUTH_REQUIRED"
