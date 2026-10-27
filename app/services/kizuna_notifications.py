"""Outbox de notificaciones a KIZUNA (kizuna_notifications).

M4 crea una notificación PENDIENTE por cada ID de transacción respondido (el principal y cada copia vinculada), en
la misma transacción que registra la respuesta, y dispara el envío después del commit. El envío en sí (HTTP,
reintentos, KIZUNA_CALLBACK_ENABLED) es M5.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations.kizuna_client import build_kizuna_payload
from app.models import Consent, KizunaNotification, NotificationStatus

logger = logging.getLogger(__name__)


async def enqueue_notifications(
    session: AsyncSession, consents: Sequence[Consent], *, settings: Settings
) -> list[uuid.UUID]:
    """Crea una notificación PENDIENTE por registro y devuelve sus IDs (para dispararlas después del commit)."""
    notifications = [
        KizunaNotification(
            transaction_id=consent.transaction_id,
            payload=build_kizuna_payload(consent),
            target_url=settings.kizuna_callback_url,
            status=NotificationStatus.PENDIENTE,
            attempts=0,
        )
        for consent in consents
    ]
    session.add_all(notifications)
    await session.flush()
    return [notification.id for notification in notifications]


async def dispatch_notifications(notification_ids: Sequence[uuid.UUID]) -> None:
    """Envía las notificaciones a KIZUNA. Se ejecuta como BackgroundTask, después del commit.

    TODO(M5): enviar cada notificación con su propia sesión (POST a target_url con reintentos) y respetar
    KIZUNA_CALLBACK_ENABLED. Mientras tanto no sale ninguna petición: las notificaciones quedan PENDIENTE.
    """
    if not notification_ids:
        return
    logger.info(
        "Notificaciones a KIZUNA registradas como PENDIENTE; el envío se implementa en M5",
        extra={"notification_ids": [str(notification_id) for notification_id in notification_ids]},
    )
