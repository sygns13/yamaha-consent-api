"""Schemas de la API pública para la landing (M4). Ninguna respuesta incluye datos personales.

Los nombres en JSON siguen el estilo de KIZUNA (camelCase; los checks con los mismos nombres que el callback, M5).
"""

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.schemas.consent import KIZUNA_MODEL_CONFIG


class LinkStatus(enum.StrEnum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"  # now > expires_at
    USED = "USED"  # used_at no nulo


class ConsentLinkInfo(BaseModel):
    """Respuesta de GET /api/v1/public/consent-links/{token}."""

    model_config = ConfigDict(
        **KIZUNA_MODEL_CONFIG,
        json_schema_extra={
            "examples": [
                {
                    "linkStatus": "VALID",
                    "privacyPolicyUrl": "https://www.yamaha.com.pe/politica-de-privacidad",
                    "dealerName": "ABC Motors",
                }
            ]
        },
    )

    link_status: LinkStatus = Field(alias="linkStatus")
    # null si PRIVACY_POLICY_URL no está configurada (TODO(PENDIENTE-14)).
    privacy_policy_url: str | None = Field(alias="privacyPolicyUrl")
    # dealerName que envió KIZUNA. null si no lo envió: la landing muestra "DEALER".
    dealer_name: str | None = Field(default=None, alias="dealerName")


class ConsentSubmitRequest(BaseModel):
    """Body de POST /api/v1/public/consent-links/{token}/submit: los tres checks de la landing.

    Booleanos estrictos (true/false de JSON): la respuesta es evidencia legal, no se interpreta "1" ni "sí". Un check
    en null (o que no se envía) se registra como false.
    """

    model_config = ConfigDict(
        **KIZUNA_MODEL_CONFIG,
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"personalDataConsent": True, "marketingConsentYMDP": False, "marketingConsentDEALER": True}
            ]
        },
    )

    personal_data: StrictBool | None = Field(default=None, alias="personalDataConsent")
    marketing_yamaha: StrictBool | None = Field(default=None, alias="marketingConsentYMDP")
    marketing_dealer: StrictBool | None = Field(default=None, alias="marketingConsentDEALER")


class ConsentSubmitResponse(BaseModel):
    status: Literal["ACEPTADO", "NO_ACEPTADO"]


class LinkGoneResponse(BaseModel):
    """410: el enlace venció o ya fue usado."""

    model_config = KIZUNA_MODEL_CONFIG

    detail: str
    link_status: Literal["EXPIRED", "USED"] = Field(alias="linkStatus")


class LinkNotFoundResponse(BaseModel):
    detail: str
