"""Helpers de base de datos para los tests de API (usan la base de pruebas de conftest.py)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Consent, ConsentStatus

Sessions = async_sessionmaker[AsyncSession]


async def all_rows(sessions: Sessions, model: type) -> list[Any]:
    async with sessions() as session:
        return list(await session.scalars(select(model).order_by(model.created_at)))


async def count(sessions: Sessions, model: type) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def get_consent(sessions: Sessions, transaction_id: str) -> Consent | None:
    async with sessions() as session:
        return await session.get(Consent, transaction_id)


async def update_consent(sessions: Sessions, transaction_id: str, **values: Any) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(Consent).where(Consent.transaction_id == transaction_id).values(**values)
        )


async def reject(sessions: Sessions, transaction_id: str) -> None:
    """Simula el resultado de RN-01 (M4): NO_ACEPTADO con borrado lógico."""
    await update_consent(
        sessions,
        transaction_id,
        status=ConsentStatus.NO_ACEPTADO,
        is_deleted=1,
        deleted_at=datetime.now(UTC),
        full_name=None,
        consent_personal_data=False,
        consent_marketing_yamaha=None,
        consent_marketing_dealer=None,
        responded_at=datetime.now(UTC),
    )


async def accept(
    sessions: Sessions,
    transaction_id: str,
    *,
    marketing_yamaha: bool = False,
    marketing_dealer: bool = True,
    responded_at: datetime | None = None,
) -> None:
    """Simula una aceptación (M4). Por defecto: marketing de Yamaha en false y del Dealer en true."""
    await update_consent(
        sessions,
        transaction_id,
        status=ConsentStatus.ACEPTADO,
        consent_personal_data=True,
        consent_marketing_yamaha=marketing_yamaha,
        consent_marketing_dealer=marketing_dealer,
        responded_at=responded_at or datetime.now(UTC),
    )
