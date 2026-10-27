"""Recepción de conformidades desde KIZUNA (M2, sección 2.3): RN-03, RN-04 y RN-05.

Todo ocurre en una sola transacción de base de datos, serializada por documento + Dealer con advisory locks de
PostgreSQL (pg_advisory_xact_lock): un SELECT ... FOR UPDATE no basta porque en el caso CREATED todavía no existe
ninguna fila que bloquear. El índice único parcial uq_consents_active_document_dealer queda como red de seguridad.
"""

import enum
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations.whatsapp import WhatsAppSender, WhatsAppSendResult
from app.models import (
    ACTION_CREATED,
    ACTION_UPDATED,
    Consent,
    ConsentLink,
    ConsentRequest,
    ConsentStatus,
    WhatsappMessage,
    WhatsappStatus,
)
from app.schemas.consent import ConsentCreateRequest
from app.utils.tokens import generate_link_token

logger = logging.getLogger(__name__)


class RegistrationResult(enum.StrEnum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"


@dataclass(frozen=True)
class RegistrationOutcome:
    result: RegistrationResult
    # Registro creado o actualizado. En RN-04 con un ID nuevo es el registro activo previo.
    consent: Consent
    # Solo en CREATED.
    whatsapp: WhatsappMessage | None = None


class TransactionAlreadyRejectedError(Exception):
    """El transaction_id ya existe y está borrado lógicamente (NO_ACEPTADO): 409, sin modificar nada.

    Guarda copias de los valores y no el objeto ORM: al salir con la excepción la transacción hace rollback y el
    objeto queda expirado (leerlo después intentaría ir a la base fuera de contexto).
    """

    def __init__(self, consent: Consent) -> None:
        super().__init__(consent.transaction_id)
        self.transaction_id = consent.transaction_id
        self.status = consent.status
        self.is_deleted = consent.is_deleted


async def register_consent(
    session: AsyncSession,
    request: ConsentCreateRequest,
    *,
    sender: WhatsAppSender,
    settings: Settings,
) -> RegistrationOutcome:
    """Registra la solicitud de KIZUNA. `request` ya viene normalizado y validado (ConsentCreateRequest)."""
    async with session.begin():
        # Siempre en este orden (transacción → documento) para que dos peticiones no se bloqueen mutuamente.
        await _advisory_lock(session, "transaction", request.transaction_id)
        await _advisory_lock(
            session, "document", request.document_type.value, request.document_number, request.dealer
        )

        # Paso 2: reenvío de la misma transacción.
        existing = await session.get(Consent, request.transaction_id, with_for_update=True)
        if existing is not None:
            if existing.is_deleted == 1:
                raise TransactionAlreadyRejectedError(existing)
            if (existing.document_type, existing.document_number, existing.dealer) != (
                request.document_type,
                request.document_number,
                request.dealer,
            ):
                # Se actualizan solo los datos del cliente (RN-04); documento y Dealer del registro se conservan.
                logger.warning(
                    "Reenvío de una transacción con documento o Dealer distintos: se conservan los del registro",
                    extra={"transaction_id": request.transaction_id},
                )
            return _update_customer_data(session, existing, request)

        # Pasos 3 y 4: registro activo del mismo documento + Dealer (RN-04 con un ID de transacción nuevo).
        active = await session.scalar(
            select(Consent)
            .where(
                Consent.document_type == request.document_type,
                Consent.document_number == request.document_number,
                Consent.dealer == request.dealer,
                Consent.is_deleted == 0,
            )
            .order_by(Consent.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if active is not None:
            return _update_customer_data(session, active, request)

        # Paso 5: no hay registro activo (nunca existió, RN-03 con otro Dealer, o RN-05 tras un rechazo).
        return await _create(session, request, sender=sender, settings=settings)


async def _advisory_lock(session: AsyncSession, namespace: str, *parts: str) -> None:
    # Clave de 64 bits derivada del SHA-256: una colisión solo serializaría de más, nunca permite duplicados.
    digest = hashlib.sha256("\x1f".join((namespace, *parts)).encode("utf-8")).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"), {"key": key})


def _update_customer_data(
    session: AsyncSession, consent: Consent, request: ConsentCreateRequest
) -> RegistrationOutcome:
    """RN-04: actualiza nombre, teléfono e información adicional. No toca las conformidades ni envía WhatsApp."""
    consent.full_name = request.full_name
    consent.phone = request.phone
    # Si KIZUNA no envía additional_info se conserva el valor anterior; si lo envía (incluso null), se reemplaza.
    if "additional_info" in request.model_fields_set:
        consent.additional_info = request.additional_info
    session.add(
        ConsentRequest(
            transaction_id=request.transaction_id,
            consent_transaction_id=consent.transaction_id,
            action=ACTION_UPDATED,
        )
    )
    logger.info(
        "Solicitud de KIZUNA: datos del cliente actualizados (RN-04)",
        extra={
            "transaction_id": request.transaction_id,
            "affected_transaction_id": consent.transaction_id,
            "result": RegistrationResult.UPDATED.value,
        },
    )
    return RegistrationOutcome(result=RegistrationResult.UPDATED, consent=consent)


async def _create(
    session: AsyncSession,
    request: ConsentCreateRequest,
    *,
    sender: WhatsAppSender,
    settings: Settings,
) -> RegistrationOutcome:
    consent = Consent(
        transaction_id=request.transaction_id,
        document_type=request.document_type,
        document_number=request.document_number,
        dealer=request.dealer,
        full_name=request.full_name,
        phone=request.phone,
        additional_info=request.additional_info,
        status=ConsentStatus.PENDIENTE,
        is_deleted=0,
    )
    session.add(consent)
    await session.flush()

    session.add(
        ConsentRequest(
            transaction_id=request.transaction_id,
            consent_transaction_id=consent.transaction_id,
            action=ACTION_CREATED,
        )
    )

    token, token_hash = generate_link_token()
    now = datetime.now(UTC)
    link = ConsentLink(
        transaction_id=consent.transaction_id,
        token_hash=token_hash,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=settings.link_ttl_hours),
    )
    session.add(link)
    await session.flush()

    to_phone = f"whatsapp:+{settings.phone_country_prefix}{consent.phone}"
    result = await _send_whatsapp(sender, to=to_phone, token=token, transaction_id=consent.transaction_id)
    message = WhatsappMessage(
        transaction_id=consent.transaction_id,
        link_id=link.id,
        twilio_message_sid=result.message_sid,
        to_phone=to_phone,
        content_sid=settings.twilio_content_sid,
        status=result.status,
        error_code=result.error_code,
        error_message=result.error_message,
    )
    session.add(message)
    await session.flush()

    logger.info(
        "Solicitud de KIZUNA: registro creado",
        extra={
            "transaction_id": consent.transaction_id,
            "result": RegistrationResult.CREATED.value,
            "whatsapp_status": result.status.value,
            "whatsapp_error_code": result.error_code,
        },
    )
    return RegistrationOutcome(result=RegistrationResult.CREATED, consent=consent, whatsapp=message)


async def _send_whatsapp(
    sender: WhatsAppSender, *, to: str, token: str, transaction_id: str
) -> WhatsAppSendResult:
    """Un fallo del envío nunca anula el registro (paso 6): queda como `failed` en whatsapp_messages."""
    try:
        return await sender.send_consent_link(to=to, token=token, transaction_id=transaction_id)
    except Exception as exc:  # noqa: BLE001 — paso 6: ningún error del envío puede anular el registro
        # Solo el tipo de error en el log: el mensaje podría incluir el teléfono.
        logger.error(
            "Error inesperado al enviar el WhatsApp",
            extra={"transaction_id": transaction_id, "error_type": type(exc).__name__},
        )
        return WhatsAppSendResult(
            status=WhatsappStatus.FAILED, error_message=f"{type(exc).__name__}: {exc}"[:1000]
        )
