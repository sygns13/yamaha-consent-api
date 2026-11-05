"""Schemas de la API para KIZUNA (M2). Normalizan y validan según la sección 2.2.

Los nombres de los campos en JSON son los del contrato de KIZUNA (IdentifierId, customerId, dealerCode...) y en
camelCase; en Python se usan los nombres en inglés y snake_case de las columnas.
"""

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
)

from app.core.config import get_settings
from app.models.consent import Consent
from app.models.enums import ConsentStatus, WhatsappStatus
from app.utils.datetimes import to_kizuna_iso
from app.utils.normalization import (
    normalize_customer_id,
    normalize_dealer,
    normalize_dealer_name,
    normalize_email,
    normalize_full_name,
    normalize_phone,
)
from app.utils.validation import (
    DEALER_NAME_MAX_LENGTH,
    is_valid_email,
    validate_customer_id,
    validate_dealer,
    validate_full_name,
    validate_phone,
)

logger = logging.getLogger(__name__)

TRANSACTION_ID_MAX_LENGTH = 100

# Entrada y salida con los nombres de KIZUNA (alias); en Python también se aceptan los nombres de los campos.
KIZUNA_MODEL_CONFIG = ConfigDict(validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)


def _not_blank_transaction_id(value: str) -> str:
    # El ID se guarda tal cual lo envía KIZUNA (es la PK y KIZUNA lo usa para consultar): no se recorta.
    if not value.strip():
        raise ValueError("El IdentifierId no puede estar vacío")
    return value


def _clean_phone(value: str) -> str:
    return validate_phone(normalize_phone(value, get_settings().phone_country_prefix))


def _lenient_text(value: object) -> object:
    # Campos opcionales que nunca rechazan la solicitud: un valor que no es texto se trata como no enviado.
    return value if isinstance(value, str) else None


def _clean_email(value: str | None) -> str | None:
    email = normalize_email(value)
    if email is not None and not is_valid_email(email):
        # No se rechaza la solicitud por el correo (decisión del dueño): se guarda null.
        logger.warning("customerEmail con formato inválido: se registra como null")
        return None
    return email


def _clean_dealer_name(value: str | None) -> str | None:
    name = normalize_dealer_name(value)
    # Solo se muestra en la landing: si es demasiado largo se recorta en lugar de rechazar la solicitud.
    return name[:DEALER_NAME_MAX_LENGTH].rstrip() if name else None


def _with_kizuna_zone(value: datetime | None) -> datetime | None:
    # Se acepta cualquier offset; sin offset se interpreta en la zona horaria de KIZUNA (KIZUNA_TIMEZONE).
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=get_settings().kizuna_zone)
    return value


TransactionId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=TRANSACTION_ID_MAX_LENGTH),
    AfterValidator(_not_blank_transaction_id),
]
CustomerId = Annotated[str, AfterValidator(lambda v: validate_customer_id(normalize_customer_id(v)))]
FullName = Annotated[str, AfterValidator(lambda v: validate_full_name(normalize_full_name(v)))]
Phone = Annotated[str, AfterValidator(_clean_phone)]
DealerCode = Annotated[str, AfterValidator(lambda v: validate_dealer(normalize_dealer(v)))]
Email = Annotated[str | None, BeforeValidator(_lenient_text), AfterValidator(_clean_email)]
DealerName = Annotated[str | None, BeforeValidator(_lenient_text), AfterValidator(_clean_dealer_name)]
RequestDateTime = Annotated[datetime | None, AfterValidator(_with_kizuna_zone)]


class ConsentRequestData(BaseModel):
    """Contenido de `_request` en POST /api/v1/consents."""

    model_config = KIZUNA_MODEL_CONFIG

    transaction_id: TransactionId = Field(alias="IdentifierId")
    customer_id: CustomerId = Field(alias="customerId")
    full_name: FullName = Field(alias="customerName")
    phone: Phone = Field(alias="customerPhone")
    # Opcional; un correo inválido se guarda como null sin rechazar la solicitud.
    email: Email = Field(default=None, alias="customerEmail")
    # Opcional: se muestra en la landing (sin él, la landing muestra "DEALER").
    dealer_name: DealerName = Field(default=None, alias="dealerName")
    dealer_code: DealerCode = Field(alias="dealerCode")
    # Opcional: si no llega, se usa la hora del sistema al recibir la solicitud.
    requested_at: RequestDateTime = Field(default=None, alias="requestDateTime")
    # Campo libre para futuras integraciones (hoy sin campos definidos).
    additional_info: dict[str, Any] | None = Field(default=None, alias="additionalInfo")


class ConsentCreateRequest(BaseModel):
    """Body de POST /api/v1/consents: KIZUNA envuelve los datos en `_request`."""

    model_config = ConfigDict(
        validate_by_name=True,
        validate_by_alias=True,
        json_schema_extra={
            "examples": [
                {
                    "_request": {
                        "IdentifierId": "100002001",
                        "customerId": "76299887",
                        "customerName": "Nombre",
                        "customerPhone": "+51999111222",
                        "customerEmail": "nombre@email.com",
                        "dealerName": "ABC Motors",
                        "dealerCode": "CL0001",
                        "requestDateTime": "2026-11-02T10:35:00-05:00",
                    }
                }
            ]
        },
    )

    request: ConsentRequestData = Field(alias="_request")


class WhatsAppSendStatus(BaseModel):
    model_config = KIZUNA_MODEL_CONFIG

    status: WhatsappStatus
    error_code: str | None = Field(alias="errorCode")


class ConsentCreateResponse(BaseModel):
    """Respuesta de POST /api/v1/consents (201 CREATED o 200 UPDATED). No incluye datos personales."""

    model_config = ConfigDict(
        **KIZUNA_MODEL_CONFIG,
        json_schema_extra={
            "examples": [
                {
                    "IdentifierId": "100002001",
                    "result": "CREATED",
                    "affectedIdentifierId": "100002001",
                    "status": "PENDIENTE",
                    "whatsapp": {"status": "queued", "errorCode": None},
                }
            ]
        },
    )

    # ID recibido en la solicitud.
    transaction_id: str = Field(alias="IdentifierId")
    result: Literal["CREATED", "UPDATED"]
    # Registro creado o actualizado. En RN-04 con un ID nuevo es el ID del registro principal activo (el ID recibido
    # queda como copia vinculada y KIZUNA recibe su propia respuesta final con ese ID, M5).
    affected_transaction_id: str = Field(alias="affectedIdentifierId")
    status: ConsentStatus
    # null cuando result es UPDATED (no se reenvía el WhatsApp).
    whatsapp: WhatsAppSendStatus | None


class ConsentConflictResponse(BaseModel):
    """409: el IdentifierId ya existe y está borrado lógicamente (NO_ACEPTADO). No se modifica nada."""

    model_config = KIZUNA_MODEL_CONFIG

    detail: str
    transaction_id: str = Field(alias="IdentifierId")
    status: ConsentStatus
    is_deleted: int = Field(alias="isDeleted")


class ConsentDetail(BaseModel):
    """Un registro en las consultas de KIZUNA (sección 2.4). No incluye nombre, teléfono ni correo.

    Los consentimientos usan los mismos nombres que el callback (M5). Son null solo mientras el registro está
    PENDIENTE. Las fechas van en la zona horaria de KIZUNA (KIZUNA_TIMEZONE).
    """

    model_config = ConfigDict(
        **KIZUNA_MODEL_CONFIG,
        json_schema_extra={
            "examples": [
                {
                    "IdentifierId": "100002001",
                    "customerId": "76299887",
                    "dealerCode": "CL0001",
                    "dealerName": "ABC Motors",
                    "status": "ACEPTADO",
                    "personalDataConsent": True,
                    "marketingConsentYMDP": False,
                    "marketingConsentDEALER": True,
                    "consentDateTime": "2026-11-02T13:35:00-05:00",
                    "requestDateTime": "2026-11-02T10:35:00-05:00",
                    "isDeleted": 0,
                    "createdAt": "2026-11-02T10:35:02-05:00",
                }
            ]
        },
    )

    transaction_id: str = Field(alias="IdentifierId")
    customer_id: str = Field(alias="customerId")
    dealer_code: str = Field(alias="dealerCode")
    dealer_name: str | None = Field(alias="dealerName")
    status: ConsentStatus
    personal_data: bool | None = Field(alias="personalDataConsent")
    marketing_yamaha: bool | None = Field(alias="marketingConsentYMDP")
    marketing_dealer: bool | None = Field(alias="marketingConsentDEALER")
    responded_at: datetime | None = Field(alias="consentDateTime")
    requested_at: datetime = Field(alias="requestDateTime")
    is_deleted: int = Field(alias="isDeleted")
    created_at: datetime = Field(alias="createdAt")

    @field_serializer("responded_at", "requested_at", "created_at")
    def _kizuna_datetime(self, value: datetime | None) -> str | None:
        return to_kizuna_iso(value, get_settings().kizuna_zone)

    @classmethod
    def from_consent(cls, consent: Consent) -> "ConsentDetail":
        # RN-01: un registro borrado lógicamente (NO_ACEPTADO) nunca expone marketing en true, aunque la base lo
        # tuviera (mismo criterio que el callback).
        deleted = consent.is_deleted == 1
        return cls(
            transaction_id=consent.transaction_id,
            customer_id=consent.customer_id,
            dealer_code=consent.dealer_code,
            dealer_name=consent.dealer_name,
            status=consent.status,
            personal_data=consent.consent_personal_data,
            marketing_yamaha=False if deleted else consent.consent_marketing_yamaha,
            marketing_dealer=False if deleted else consent.consent_marketing_dealer,
            responded_at=consent.responded_at,
            requested_at=consent.requested_at,
            is_deleted=consent.is_deleted,
            created_at=consent.created_at,
        )


class ConsentListQuery(BaseModel):
    """Query de GET /api/v1/consents. El customerId se normaliza igual que al registrarlo."""

    model_config = KIZUNA_MODEL_CONFIG

    customer_id: CustomerId = Field(alias="customerId")
    dealer_code: DealerCode | None = Field(default=None, alias="dealerCode")
