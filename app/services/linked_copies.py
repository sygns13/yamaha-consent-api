"""Copias vinculadas (RN-04 con un ID de transacción nuevo): reciben la respuesta del registro principal.

M4 (app/services/consent_links.py) llama a apply_response_to_linked_copies dentro de la misma transacción en la que registra la respuesta del
cliente en el principal, con la fila del principal bloqueada (SELECT ... FOR UPDATE). M2 bloquea esa misma fila
antes de crear una copia, así que una copia nueva o bien ve la respuesta ya registrada, o bien existe cuando M4
propaga: ninguna queda PENDIENTE para siempre.
"""

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Consent, ConsentStatus

logger = logging.getLogger(__name__)


async def apply_response_to_linked_copies(session: AsyncSession, primary: Consent) -> Sequence[Consent]:
    """Copia la respuesta de `primary` a sus copias vinculadas que siguen PENDIENTE y las devuelve.

    - ACEPTADO: se copian el estado, los tres checks y responded_at.
    - NO_ACEPTADO (RN-01): además, borrado lógico de cada copia (is_deleted = 1, deleted_at), nombre anulado y
      marketing en null. Documento, Dealer, teléfono y transacción se conservan.

    El llamador crea una notificación a KIZUNA por cada copia devuelta (una respuesta por cada ID de transacción).
    """
    if primary.status == ConsentStatus.PENDIENTE:
        raise ValueError("El registro principal todavía no tiene respuesta")

    copies = (
        await session.scalars(
            select(Consent)
            .where(
                Consent.source_transaction_id == primary.transaction_id,
                Consent.status == ConsentStatus.PENDIENTE,
                Consent.is_deleted == 0,
            )
            .order_by(Consent.created_at, Consent.transaction_id)
            .with_for_update()
        )
    ).all()

    for copy in copies:
        copy.status = primary.status
        copy.consent_personal_data = primary.consent_personal_data
        copy.consent_marketing_yamaha = primary.consent_marketing_yamaha
        copy.consent_marketing_dealer = primary.consent_marketing_dealer
        copy.responded_at = primary.responded_at
        if primary.status == ConsentStatus.NO_ACEPTADO:
            copy.is_deleted = 1
            copy.deleted_at = primary.deleted_at or primary.responded_at
            copy.full_name = None
            copy.consent_marketing_yamaha = None
            copy.consent_marketing_dealer = None

    if copies:
        logger.info(
            "Respuesta del cliente copiada a las transacciones vinculadas",
            extra={
                "transaction_id": primary.transaction_id,
                "linked_transaction_ids": [copy.transaction_id for copy in copies],
                "status": primary.status.value,
            },
        )
    return copies
