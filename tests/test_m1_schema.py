"""Criterios de aceptación de M1 contra la base local (requiere `alembic upgrade head`).

Cada test corre dentro de una transacción que se revierte al final: no deja datos.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.models import (
    ACTION_CREATED,
    Consent,
    ConsentEvidence,
    ConsentLink,
    ConsentRequest,
    ConsentStatus,
    DocumentType,
    KizunaNotification,
    NotificationStatus,
    WhatsappMessage,
    WhatsappMessageEvent,
    WhatsappStatus,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
    try:
        conn = await engine.connect()
    except (OSError, OperationalError) as exc:
        await engine.dispose()
        pytest.skip(f"Base de datos no disponible: {exc}")
    migrated = await conn.scalar(text("SELECT to_regclass('consents')"))
    await conn.rollback()
    if not migrated:
        await conn.close()
        await engine.dispose()
        pytest.skip("Esquema no migrado: ejecutar `alembic upgrade head`")

    trans = await conn.begin()
    db = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


def make_consent(transaction_id: str | None = None, **overrides) -> Consent:
    values = {
        "transaction_id": transaction_id or f"TEST-{uuid.uuid4().hex[:12]}",
        "document_type": DocumentType.DNI,
        "document_number": "12345678",
        "dealer": "DEALER-TEST",
        "full_name": "JUAN PÉREZ GÓMEZ",
        "phone": "987654321",
    }
    values.update(overrides)
    return Consent(**values)


async def test_duplicate_transaction_id_fails(session: AsyncSession) -> None:
    session.add(make_consent("TEST-DUP"))
    await session.flush()

    session.add(make_consent("TEST-DUP", dealer="DEALER-OTRO"))
    with pytest.raises(IntegrityError, match="pk_consents"):
        await session.flush()


async def test_same_document_and_dealer_allowed_when_previous_is_deleted(session: AsyncSession) -> None:
    session.add(make_consent("TEST-OLD", is_deleted=1, full_name=None, status=ConsentStatus.NO_ACEPTADO))
    await session.flush()

    session.add(make_consent("TEST-NEW"))
    await session.flush()

    rows = await session.scalars(
        select(Consent.transaction_id).where(
            Consent.document_number == "12345678", Consent.dealer == "DEALER-TEST"
        )
    )
    assert set(rows) == {"TEST-OLD", "TEST-NEW"}


async def test_two_active_rows_for_same_document_and_dealer_fail(session: AsyncSession) -> None:
    session.add(make_consent("TEST-A1"))
    await session.flush()

    session.add(make_consent("TEST-A2"))
    with pytest.raises(IntegrityError, match="uq_consents_active_document_dealer"):
        await session.flush()


async def test_active_linked_copies_are_allowed_next_to_the_active_primary(session: AsyncSession) -> None:
    # RN-04 con un ID nuevo: el índice único parcial solo cuenta los registros principales.
    session.add(make_consent("TEST-P"))
    await session.flush()
    session.add_all(
        [
            make_consent("TEST-C1", source_transaction_id="TEST-P"),
            make_consent("TEST-C2", source_transaction_id="TEST-P"),
        ]
    )
    await session.flush()


async def test_linked_copy_cannot_point_to_itself(session: AsyncSession) -> None:
    session.add(make_consent("TEST-SELF"))
    await session.flush()
    with pytest.raises(IntegrityError, match="ck_consents_source_not_self"):
        await session.execute(
            text(
                "UPDATE consents SET source_transaction_id = transaction_id WHERE transaction_id = 'TEST-SELF'"
            )
        )


async def test_linked_copy_must_point_to_an_existing_record(session: AsyncSession) -> None:
    session.add(make_consent("TEST-ORPHAN", source_transaction_id="TEST-NO-EXISTE"))
    with pytest.raises(IntegrityError, match="fk_consents_source_transaction_id_consents"):
        await session.flush()


async def test_same_document_with_other_dealer_allowed(session: AsyncSession) -> None:
    session.add_all([make_consent("TEST-D1"), make_consent("TEST-D2", dealer="DEALER-OTRO")])
    await session.flush()


@pytest.mark.parametrize("value", [2, -1])
async def test_is_deleted_rejects_values_other_than_0_and_1(session: AsyncSession, value: int) -> None:
    session.add(make_consent(is_deleted=value))
    with pytest.raises(IntegrityError, match="ck_consents_is_deleted_bool"):
        await session.flush()


async def test_consent_defaults(session: AsyncSession) -> None:
    consent = make_consent()
    session.add(consent)
    await session.flush()
    await session.refresh(consent)

    assert consent.status == ConsentStatus.PENDIENTE
    assert consent.is_deleted == 0
    assert consent.created_at.tzinfo is not None
    assert consent.updated_at.tzinfo is not None


async def test_invalid_enum_value_fails(session: AsyncSession) -> None:
    with pytest.raises(DBAPIError):
        await session.execute(
            text(
                "INSERT INTO consents (transaction_id, document_type, document_number, dealer, phone) "
                "VALUES ('TEST-ENUM', 'LIBRETA', '1', 'D', '987654321')"
            )
        )


async def test_full_graph_insert(session: AsyncSession) -> None:
    """Inserta una fila en cada tabla para validar FKs, tipos (inet, jsonb, uuid) y enums."""
    consent = make_consent(additional_info={})
    session.add(consent)
    await session.flush()

    now = datetime.now(UTC)
    link = ConsentLink(
        transaction_id=consent.transaction_id, token_hash="a" * 64, expires_at=now + timedelta(hours=168)
    )
    session.add_all(
        [
            ConsentRequest(
                transaction_id=consent.transaction_id,
                consent_transaction_id=consent.transaction_id,
                action=ACTION_CREATED,
            ),
            link,
        ]
    )
    await session.flush()

    message = WhatsappMessage(
        transaction_id=consent.transaction_id,
        link_id=link.id,
        twilio_message_sid=f"SM{uuid.uuid4().hex}",
        to_phone="whatsapp:+51987654321",
        content_sid="HXtest",
        status=WhatsappStatus.QUEUED,
    )
    session.add_all(
        [
            message,
            ConsentEvidence(
                transaction_id=consent.transaction_id,
                link_id=link.id,
                ip_address="203.0.113.10",
                user_agent="pytest",
                policy_version="v1",
                policy_url="https://example.com/privacidad",
                answers={"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
                submitted_at=now,
            ),
            KizunaNotification(
                transaction_id=consent.transaction_id,
                payload={"transaction_id": consent.transaction_id},
                target_url="http://kizuna-mock:8081/kizuna/consent-result",
            ),
        ]
    )
    await session.flush()

    event = WhatsappMessageEvent(
        message_id=message.id, status=WhatsappStatus.DELIVERED, raw_payload={"MessageStatus": "delivered"}
    )
    session.add(event)
    await session.flush()

    stored = await session.scalar(
        text("SELECT status::text FROM whatsapp_message_events WHERE id = :id"), {"id": event.id}
    )
    assert stored == "delivered"
    notification = await session.scalar(
        select(KizunaNotification).where(KizunaNotification.transaction_id == consent.transaction_id)
    )
    assert notification.status == NotificationStatus.PENDIENTE
    assert notification.attempts == 0


async def test_link_requires_existing_consent(session: AsyncSession) -> None:
    session.add(
        ConsentLink(transaction_id="TEST-NO-EXISTE", token_hash="b" * 64, expires_at=datetime.now(UTC))
    )
    with pytest.raises(IntegrityError, match="fk_consent_links_transaction_id_consents"):
        await session.flush()
