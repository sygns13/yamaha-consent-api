"""Outbox de notificaciones a KIZUNA (kizuna_notifications) y envío con reintentos (M5).

- Cada ID de transacción respondido (el principal y cada copia vinculada, RN-04) tiene su propia notificación, con
  su propio transaction_id y reintentos independientes.
- La notificación se crea PENDIENTE en la misma transacción que la respuesta, y el envío se dispara después del
  commit (BackgroundTasks): si KIZUNA no responde, la respuesta del cliente ya quedó registrada.
- 1 intento + KIZUNA_CALLBACK_MAX_RETRIES reintentos, con KIZUNA_CALLBACK_BACKOFF_SECONDS de espera antes de cada
  reintento. Se reintenta ante error de red, timeout, 5xx y 429; cualquier otra respuesta que no sea 2xx → FALLIDO.
- Con KIZUNA_CALLBACK_ENABLED=false no sale ninguna petición: la notificación queda PENDIENTE (sin reclamar).
- Reclamo (claimed_at, SELECT ... FOR UPDATE SKIP LOCKED): una sola instancia envía cada notificación. El reclamo se
  renueva en cada intento; si pasan KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES sin renovarse, vence.
- Al arrancar con el envío encendido se reprocesan las PENDIENTE sin reclamar (todas, antiguas y recientes) y las de
  reclamo vencido.

Durante el envío y la espera entre intentos no se mantiene abierta ninguna transacción: cada intento se guarda con
commit.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
from fastapi import Depends
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.integrations.kizuna_client import KizunaClient, build_kizuna_payload
from app.models import Consent, KizunaNotification, NotificationStatus

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

# Envíos simultáneos en el reproceso al arrancar (para no saturar a KIZUNA ni el pool de conexiones).
REPROCESS_CONCURRENCY = 10


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


def backoff_seconds(retry_number: int, backoff: Sequence[float]) -> float:
    """Espera antes del reintento número `retry_number` (1, 2, ...).

    Si hay más reintentos que valores en KIZUNA_CALLBACK_BACKOFF_SECONDS se repite el último; sin valores, 0.
    """
    if not backoff:
        return 0.0
    return float(backoff[min(retry_number, len(backoff)) - 1])


class KizunaNotifier:
    """Envía las notificaciones a KIZUNA: después del commit (BackgroundTask) y en el reproceso al arrancar."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        # Los tests pasan el mock de KIZUNA como transporte ASGI (sin red).
        self._transport = transport
        self._sleep = sleep

    async def dispatch(self, notification_ids: Sequence[uuid.UUID]) -> None:
        """Envía las notificaciones recién creadas (BackgroundTask, después del commit)."""
        if not notification_ids:
            return
        if not self._settings.kizuna_callback_enabled:
            # Sin reclamarlas: el reproceso al arrancar con el envío encendido las enviará.
            logger.info(
                "KIZUNA_CALLBACK_ENABLED=false: no se envía la respuesta a KIZUNA; la notificación queda PENDIENTE",
                extra={"notification_ids": [str(notification_id) for notification_id in notification_ids]},
            )
            return
        await self._send(await self.claim(notification_ids))

    async def reprocess_pending(self) -> int:
        """Al arrancar: envía todas las PENDIENTE sin reclamar (también las acumuladas con el envío apagado, antiguas
        y recientes) y las que llevan más de KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES sin intento (la instancia que las
        enviaba se reinició). Devuelve cuántas reclamó.
        """
        if not self._settings.kizuna_callback_enabled:
            return 0
        claimed = await self.claim()
        logger.info(
            "Reproceso de notificaciones a KIZUNA pendientes", extra={"notifications_claimed": len(claimed)}
        )
        await self._send(claimed, concurrency=REPROCESS_CONCURRENCY)
        return len(claimed)

    async def claim(self, notification_ids: Sequence[uuid.UUID] | None = None) -> list[uuid.UUID]:
        """Reclama las notificaciones PENDIENTE sin reclamar o con el reclamo vencido (todas, o solo las indicadas).

        SELECT ... FOR UPDATE SKIP LOCKED en una transacción corta: si dos instancias reclaman a la vez, cada
        notificación queda para una sola. Las que otra instancia ya reclamó (con el reclamo vigente) no se devuelven.
        """
        now = datetime.now(UTC)
        expired = now - timedelta(minutes=self._settings.kizuna_callback_reprocess_after_minutes)
        query = (
            select(KizunaNotification.id)
            .where(
                KizunaNotification.status == NotificationStatus.PENDIENTE,
                or_(KizunaNotification.claimed_at.is_(None), KizunaNotification.claimed_at < expired),
            )
            .order_by(KizunaNotification.created_at)
            .with_for_update(skip_locked=True)
        )
        if notification_ids is not None:
            query = query.where(KizunaNotification.id.in_(notification_ids))

        async with self._session_factory.begin() as session:
            claimed = list(await session.scalars(query))
            if claimed:
                await session.execute(
                    update(KizunaNotification)
                    .where(KizunaNotification.id.in_(claimed))
                    .values(claimed_at=now)
                )
        return claimed

    async def _send(self, notification_ids: Sequence[uuid.UUID], *, concurrency: int | None = None) -> None:
        if not notification_ids:
            return
        limit = asyncio.Semaphore(concurrency or len(notification_ids))

        async def deliver_limited(client: KizunaClient, notification_id: uuid.UUID) -> None:
            async with limit:
                await self._deliver_safely(client, notification_id)

        async with KizunaClient.build_http_client(self._settings, self._transport) as http_client:
            client = KizunaClient.from_settings(http_client, self._settings)
            # En paralelo: los reintentos de una notificación no retrasan a las demás.
            await asyncio.gather(
                *(deliver_limited(client, notification_id) for notification_id in notification_ids)
            )

    async def _deliver_safely(self, client: KizunaClient, notification_id: uuid.UUID) -> None:
        try:
            await self.deliver(client, notification_id)
        except Exception as exc:  # noqa: BLE001 — una notificación con error (p. ej. la base) no corta a las demás
            # Queda PENDIENTE con el último intento guardado; el reproceso la retoma cuando venza su reclamo. Solo el
            # tipo de error: el mensaje podría traer datos.
            logger.error(
                "Error inesperado al notificar a KIZUNA",
                extra={"notification_id": str(notification_id), "error_type": type(exc).__name__},
            )

    async def deliver(self, client: KizunaClient, notification_id: uuid.UUID) -> NotificationStatus | None:
        """Envía una notificación PENDIENTE ya reclamada, con sus reintentos. Devuelve el estado final (None si no
        existe).
        """
        max_attempts = 1 + self._settings.kizuna_callback_max_retries
        async with self._session_factory() as session:
            notification = await session.get(KizunaNotification, notification_id)
            if notification is None:
                logger.warning(
                    "Notificación a KIZUNA inexistente", extra={"notification_id": str(notification_id)}
                )
                return None
            if notification.status != NotificationStatus.PENDIENTE:
                return notification.status
            # Cierra la transacción de lectura: durante el envío no se mantiene ninguna abierta.
            await session.commit()

            log_extra = {
                "notification_id": str(notification.id),
                "transaction_id": notification.transaction_id,
            }
            while True:
                result = await client.send(notification.target_url, notification.payload)
                now = datetime.now(UTC)
                notification.attempts += 1
                notification.last_attempt_at = now
                # Renueva el reclamo: mientras siga reintentando, ninguna otra instancia la toma.
                notification.claimed_at = now
                notification.last_http_status = result.http_status
                notification.last_error = result.error
                attempt_extra = {
                    **log_extra,
                    "attempt": notification.attempts,
                    "max_attempts": max_attempts,
                    "http_status": result.http_status,
                }

                if result.ok:
                    notification.status = NotificationStatus.ENVIADO
                    notification.sent_at = now
                    await session.commit()
                    logger.info("Respuesta enviada a KIZUNA", extra=attempt_extra)
                    return notification.status

                if not result.retryable or notification.attempts >= max_attempts:
                    notification.status = NotificationStatus.FALLIDO
                    await session.commit()
                    reason = "se agotaron los reintentos" if result.retryable else "respuesta no reintentable"
                    # La consulta de KIZUNA (M2) sigue siendo la vía de respaldo.
                    logger.error("No se pudo enviar la respuesta a KIZUNA: %s", reason, extra=attempt_extra)
                    return notification.status

                await session.commit()
                wait = backoff_seconds(notification.attempts, self._settings.kizuna_callback_backoff_seconds)
                logger.warning(
                    "Falló el envío a KIZUNA; se reintentará",
                    extra={**attempt_extra, "retry_in_seconds": wait},
                )
                await self._sleep(wait)


@asynccontextmanager
async def reprocess_pending_in_background(notifier: KizunaNotifier) -> AsyncIterator[asyncio.Task[None]]:
    """Lanza el reproceso al arrancar sin esperarlo y lo cancela al apagar la app.

    Lo que quede a medias al apagar conserva su reclamo y se retoma en el próximo arranque, cuando el reclamo venza.
    """

    async def run() -> None:
        try:
            await notifier.reprocess_pending()
        except Exception as exc:  # noqa: BLE001 — un fallo del reproceso (p. ej. la base) no debe tumbar la app
            logger.error(
                "Error en el reproceso de notificaciones a KIZUNA", extra={"error_type": type(exc).__name__}
            )

    task = asyncio.create_task(run())
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def get_kizuna_notifier(
    settings: Annotated[Settings, Depends(get_settings)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> KizunaNotifier:
    return KizunaNotifier(settings, session_factory)
