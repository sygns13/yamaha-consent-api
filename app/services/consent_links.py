"""API pública para la landing (M4): validación del enlace y registro de las respuestas del cliente (RN-01).

El enlace se busca por sha256(token): el token en claro nunca se guarda ni se loguea. El envío ocurre en una sola
transacción con la fila del enlace bloqueada (SELECT ... FOR UPDATE), así que de dos envíos simultáneos solo uno se
registra: el segundo espera, ve used_at y recibe 410 USED.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Consent, ConsentEvidence, ConsentLink, ConsentStatus
from app.schemas.public import ConsentSubmitRequest, LinkStatus
from app.services.kizuna_notifications import enqueue_notifications
from app.services.linked_copies import apply_response_to_linked_copies
from app.utils.tokens import hash_token

logger = logging.getLogger(__name__)


class LinkNotFoundError(Exception):
    """No existe un enlace con ese token: 404."""


class LinkUnavailableError(Exception):
    """El enlace venció o ya fue usado: 410 con su link_status."""

    def __init__(self, link_status: LinkStatus) -> None:
        super().__init__(link_status.value)
        self.link_status = link_status


@dataclass(frozen=True)
class SubmissionEvidence:
    """Datos de la petición que se guardan como evidencia. Son datos personales: no se loguean."""

    ip_address: str | None
    user_agent: str | None


@dataclass(frozen=True)
class SubmissionOutcome:
    status: ConsentStatus
    transaction_id: str
    # Copias vinculadas que recibieron la respuesta (RN-04).
    linked_transaction_ids: list[str]
    # Una notificación a KIZUNA por cada ID de transacción respondido; se disparan después del commit.
    notification_ids: list[uuid.UUID]


def link_status(link: ConsentLink, now: datetime) -> LinkStatus:
    # Un enlace usado es USED aunque además haya vencido: el cliente ya respondió.
    if link.used_at is not None:
        return LinkStatus.USED
    if now > link.expires_at:
        return LinkStatus.EXPIRED
    return LinkStatus.VALID


async def get_link_status(session: AsyncSession, token: str) -> LinkStatus | None:
    """Estado del enlace, o None si el token no existe."""
    link = await session.scalar(select(ConsentLink).where(ConsentLink.token_hash == hash_token(token)))
    if link is None:
        return None
    return link_status(link, datetime.now(UTC))


async def submit_consent(
    session: AsyncSession,
    token: str,
    answers: ConsentSubmitRequest,
    *,
    evidence: SubmissionEvidence,
    settings: Settings,
) -> SubmissionOutcome:
    """Registra la respuesta del cliente en el registro del enlace y en sus copias vinculadas PENDIENTE."""
    async with session.begin():
        # Paso 2: el bloqueo va antes de validar; si no, dos envíos simultáneos verían el enlace sin usar.
        link = await session.scalar(
            select(ConsentLink).where(ConsentLink.token_hash == hash_token(token)).with_for_update()
        )
        if link is None:
            raise LinkNotFoundError

        # Paso 1.
        now = datetime.now(UTC)
        status = link_status(link, now)
        if status is not LinkStatus.VALID:
            logger.info(
                "Envío rechazado: el enlace no está vigente",
                extra={"transaction_id": link.transaction_id, "link_status": status.value},
            )
            raise LinkUnavailableError(status)

        # Fila del principal bloqueada: POST /consents la bloquea antes de crear una copia vinculada (RN-04), así
        # que una copia nueva o ve esta respuesta ya registrada o existe cuando se propaga en el paso 7.
        consent = await session.get(Consent, link.transaction_id, with_for_update=True)

        # Paso 3: la evidencia guarda lo que el cliente envió, tal cual.
        session.add(
            ConsentEvidence(
                transaction_id=consent.transaction_id,
                link_id=link.id,
                ip_address=evidence.ip_address,
                user_agent=evidence.user_agent,
                policy_version=settings.privacy_policy_version,
                # TODO(PENDIENTE-14): URL y versión vigentes de la política de privacidad.
                policy_url=settings.privacy_policy_url,
                answers=answers.model_dump(),
                submitted_at=now,
            )
        )

        # Pasos 4 a 6.
        _apply_answers(consent, answers, now)
        link.used_at = now

        # Paso 7: copias vinculadas PENDIENTE (RN-04).
        copies = await apply_response_to_linked_copies(session, consent)

        # Paso 8: una notificación por el principal y una por cada copia actualizada.
        notification_ids = await enqueue_notifications(session, [consent, *copies], settings=settings)

        logger.info(
            "Respuesta del cliente registrada",
            extra={
                "transaction_id": consent.transaction_id,
                "status": consent.status.value,
                "linked_transaction_ids": [copy.transaction_id for copy in copies],
            },
        )
        return SubmissionOutcome(
            status=consent.status,
            transaction_id=consent.transaction_id,
            linked_transaction_ids=[copy.transaction_id for copy in copies],
            notification_ids=notification_ids,
        )


def _apply_answers(consent: Consent, answers: ConsentSubmitRequest, now: datetime) -> None:
    consent.consent_personal_data = answers.personal_data
    consent.responded_at = now
    if answers.personal_data:
        # Paso 4.
        consent.status = ConsentStatus.ACEPTADO
        consent.consent_marketing_yamaha = answers.marketing_yamaha
        consent.consent_marketing_dealer = answers.marketing_dealer
        return

    # Paso 5, RN-01: borrado lógico. Solo se anula el nombre; el marketing no se conserva. Se mantienen documento,
    # Dealer, teléfono (trazabilidad), transacción, additional_info, evidencia y WhatsApp.
    consent.status = ConsentStatus.NO_ACEPTADO
    consent.is_deleted = 1
    consent.deleted_at = now
    consent.full_name = None
    consent.consent_marketing_yamaha = None
    consent.consent_marketing_dealer = None
