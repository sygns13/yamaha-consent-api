"""Modelos SQLAlchemy. Importar este paquete registra todas las tablas en Base.metadata (lo usa Alembic)."""

from app.models.consent import Consent
from app.models.consent_evidence import ConsentEvidence
from app.models.consent_link import ConsentLink
from app.models.consent_request import ACTION_CREATED, ACTION_UPDATED, ConsentRequest
from app.models.enums import ConsentStatus, DocumentType, NotificationStatus, WhatsappStatus
from app.models.kizuna_notification import KizunaNotification
from app.models.whatsapp import WhatsappMessage, WhatsappMessageEvent

__all__ = [
    "ACTION_CREATED",
    "ACTION_UPDATED",
    "Consent",
    "ConsentEvidence",
    "ConsentLink",
    "ConsentRequest",
    "ConsentStatus",
    "DocumentType",
    "KizunaNotification",
    "NotificationStatus",
    "WhatsappMessage",
    "WhatsappMessageEvent",
    "WhatsappStatus",
]
