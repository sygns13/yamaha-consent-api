"""Schemas de la API pública para la landing (M4). Ninguna respuesta incluye datos personales."""

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool


class LinkStatus(enum.StrEnum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"  # now > expires_at
    USED = "USED"  # used_at no nulo


class ConsentLinkInfo(BaseModel):
    """Respuesta de GET /api/v1/public/consent-links/{token}."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "link_status": "VALID",
                    "privacy_policy_url": "https://www.yamaha.com.pe/politica-de-privacidad",
                    "dealer_display_name": None,
                }
            ]
        }
    )

    link_status: LinkStatus
    # null si PRIVACY_POLICY_URL no está configurada (TODO(PENDIENTE-14)).
    privacy_policy_url: str | None
    # TODO(PENDIENTE-05): ¿se muestra el nombre del Dealer? Mientras tanto null y la landing muestra "DEALER".
    dealer_display_name: None = None


class ConsentSubmitRequest(BaseModel):
    """Body de POST /api/v1/public/consent-links/{token}/submit: los tres checks de la landing.

    Booleanos estrictos (true/false de JSON): la respuesta es evidencia legal, no se interpreta "1" ni "sí".
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"personal_data": True, "marketing_yamaha": False, "marketing_dealer": True}]
        },
    )

    personal_data: StrictBool
    marketing_yamaha: StrictBool
    marketing_dealer: StrictBool


class ConsentSubmitResponse(BaseModel):
    status: Literal["ACEPTADO", "NO_ACEPTADO"]


class LinkGoneResponse(BaseModel):
    """410: el enlace venció o ya fue usado."""

    detail: str
    link_status: Literal["EXPIRED", "USED"]


class LinkNotFoundResponse(BaseModel):
    detail: str
