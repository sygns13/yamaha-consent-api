"""Consultas de KIZUNA (M2, sección 2.4). Solo lectura: incluyen los registros borrados lógicamente."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Consent
from app.schemas.consent import ConsentListQuery


async def get_consent(session: AsyncSession, transaction_id: str) -> Consent | None:
    """Por IdentifierId, exacto. Un ID nuevo absorbido por RN-04 no es un registro: devuelve None."""
    return await session.get(Consent, transaction_id)


async def list_consents(session: AsyncSession, query: ConsentListQuery) -> Sequence[Consent]:
    """Registros del cliente (y del Dealer, si se indica), del más reciente al más antiguo.

    `query` ya viene normalizado (ConsentListQuery), igual que al registrar. A igual created_at se desempata por
    transaction_id para que el orden sea estable.
    """
    statement = select(Consent).where(Consent.customer_id == query.customer_id)
    if query.dealer_code is not None:
        statement = statement.where(Consent.dealer_code == query.dealer_code)
    statement = statement.order_by(Consent.created_at.desc(), Consent.transaction_id.desc())
    return (await session.scalars(statement)).all()
