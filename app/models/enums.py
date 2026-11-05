import enum

from sqlalchemy import Enum


class ConsentStatus(enum.StrEnum):
    PENDIENTE = "PENDIENTE"
    ACEPTADO = "ACEPTADO"
    NO_ACEPTADO = "NO_ACEPTADO"


class WhatsappStatus(enum.StrEnum):
    """Valores de MessageStatus de Twilio."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    UNDELIVERED = "undelivered"


class NotificationStatus(enum.StrEnum):
    PENDIENTE = "PENDIENTE"
    ENVIADO = "ENVIADO"
    FALLIDO = "FALLIDO"


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # Guarda el valor (no el nombre del miembro): whatsapp_status se persiste en minúsculas como en Twilio.
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


consent_status_enum = _pg_enum(ConsentStatus, "consent_status")
whatsapp_status_enum = _pg_enum(WhatsappStatus, "whatsapp_status")
notification_status_enum = _pg_enum(NotificationStatus, "notification_status")
