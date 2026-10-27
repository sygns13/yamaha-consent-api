"""Schemas de entrada de la API para KIZUNA (M2). Normalizan y validan según la sección 2.2."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    field_validator,
)
from pydantic_core.core_schema import ValidationInfo

from app.core.config import get_settings
from app.models.consent import Consent
from app.models.enums import ConsentStatus, DocumentType, WhatsappStatus
from app.utils.normalization import (
    normalize_dealer,
    normalize_document_number,
    normalize_document_type,
    normalize_full_name,
    normalize_phone,
)
from app.utils.validation import (
    validate_dealer,
    validate_document_number,
    validate_full_name,
    validate_phone,
)

TRANSACTION_ID_MAX_LENGTH = 100


def _document_type_before(value: object) -> object:
    # Solo se normalizan textos; cualquier otro tipo lo rechaza la validación del enum.
    return normalize_document_type(value) if isinstance(value, str) else value


def _not_blank_transaction_id(value: str) -> str:
    # El ID se guarda tal cual lo envía KIZUNA (es la PK y KIZUNA lo usa para consultar): no se recorta.
    if not value.strip():
        raise ValueError("El ID de transacción no puede estar vacío")
    return value


def _clean_phone(value: str) -> str:
    return validate_phone(normalize_phone(value, get_settings().phone_country_prefix))


TransactionId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=TRANSACTION_ID_MAX_LENGTH),
    AfterValidator(_not_blank_transaction_id),
]
DocumentTypeField = Annotated[DocumentType, BeforeValidator(_document_type_before)]
FullName = Annotated[str, AfterValidator(lambda v: validate_full_name(normalize_full_name(v)))]
Phone = Annotated[str, AfterValidator(_clean_phone)]
Dealer = Annotated[str, AfterValidator(lambda v: validate_dealer(normalize_dealer(v)))]


def _clean_document_number(value: str, info: ValidationInfo) -> str:
    """Normaliza y valida según document_type (que se declara antes y ya está validado en info.data).

    Si document_type es inválido no está en info.data: su error ya se reporta y aquí solo se normaliza.
    """
    normalized = normalize_document_number(value)
    document_type = info.data.get("document_type")
    if document_type is None:
        return normalized
    return validate_document_number(document_type, normalized)


class ConsentCreateRequest(BaseModel):
    """Body de POST /api/v1/consents."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transaction_id": "TRX-000123",
                    "document_type": "DNI",
                    "document_number": "12345678",
                    "full_name": "juan  pérez   gómez",
                    "phone": "+51 987 654 321",
                    "dealer": "DEALER-001",
                    "additional_info": {},
                }
            ]
        }
    )

    transaction_id: TransactionId
    # document_type debe ir antes de document_number: su validación depende de él.
    document_type: DocumentTypeField
    document_number: str
    full_name: FullName
    phone: Phone
    dealer: Dealer
    # Campo libre para futuras integraciones (hoy sin campos definidos).
    additional_info: dict[str, Any] | None = None

    _document_number = field_validator("document_number")(_clean_document_number)


class WhatsAppSendStatus(BaseModel):
    status: WhatsappStatus
    error_code: str | None


class ConsentCreateResponse(BaseModel):
    """Respuesta de POST /api/v1/consents (201 CREATED o 200 UPDATED). No incluye datos personales."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transaction_id": "TRX-000123",
                    "result": "CREATED",
                    "affected_transaction_id": "TRX-000123",
                    "status": "PENDIENTE",
                    "whatsapp": {"status": "queued", "error_code": None},
                }
            ]
        }
    )

    # ID recibido en la solicitud.
    transaction_id: str
    result: Literal["CREATED", "UPDATED"]
    # Registro creado o actualizado. En RN-04 con un ID nuevo es el ID del registro principal activo (el ID recibido
    # queda como copia vinculada y KIZUNA recibe su propia respuesta final con ese ID, M5).
    affected_transaction_id: str
    status: ConsentStatus
    # null cuando result es UPDATED (no se reenvía el WhatsApp).
    whatsapp: WhatsAppSendStatus | None


class ConsentConflictResponse(BaseModel):
    """409: el transaction_id ya existe y está borrado lógicamente (NO_ACEPTADO). No se modifica nada."""

    detail: str
    transaction_id: str
    status: ConsentStatus
    is_deleted: int


class ConsentAnswers(BaseModel):
    personal_data: bool | None
    marketing_yamaha: bool | None
    marketing_dealer: bool | None


class ConsentDetail(BaseModel):
    """Un registro en las consultas de KIZUNA (sección 2.4). No incluye nombre ni teléfono."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transaction_id": "TRX-000123",
                    "document_type": "DNI",
                    "document_number": "12345678",
                    "dealer": "DEALER-001",
                    "status": "ACEPTADO",
                    "consents": {"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True},
                    "responded_at": "2026-11-05T15:20:11Z",
                    "is_deleted": 0,
                    "created_at": "2026-11-04T10:00:00Z",
                }
            ]
        }
    )

    transaction_id: str
    document_type: DocumentType
    document_number: str
    dealer: str
    status: ConsentStatus
    consents: ConsentAnswers
    responded_at: datetime | None
    is_deleted: int
    created_at: datetime

    @classmethod
    def from_consent(cls, consent: Consent) -> "ConsentDetail":
        # RN-01: en un registro borrado lógicamente (NO_ACEPTADO) no se exponen las respuestas de marketing,
        # aunque la base tuviera algún valor.
        deleted = consent.is_deleted == 1
        return cls(
            transaction_id=consent.transaction_id,
            document_type=consent.document_type,
            document_number=consent.document_number,
            dealer=consent.dealer,
            status=consent.status,
            consents=ConsentAnswers(
                personal_data=consent.consent_personal_data,
                marketing_yamaha=None if deleted else consent.consent_marketing_yamaha,
                marketing_dealer=None if deleted else consent.consent_marketing_dealer,
            ),
            responded_at=consent.responded_at,
            is_deleted=consent.is_deleted,
            created_at=consent.created_at,
        )


class ConsentListQuery(BaseModel):
    """Query de GET /api/v1/consents. El documento se normaliza igual que al registrarlo."""

    document_type: DocumentTypeField
    document_number: str
    dealer: Dealer | None = None

    _document_number = field_validator("document_number")(_clean_document_number)
