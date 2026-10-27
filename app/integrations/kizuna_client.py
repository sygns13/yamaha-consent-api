"""Callback a KIZUNA (M5). Por ahora solo el payload: el envío HTTP con reintentos se implementa en M5.

TODO(PENDIENTE-13): el contrato real (URL, autenticación, campos y literales de estado) lo define Yamaha/KIZUNA.
Pasar al endpoint real solo debe tocar las variables de entorno y build_kizuna_payload().
"""

from datetime import UTC, datetime
from typing import Any

from app.models import Consent


def build_kizuna_payload(consent: Consent) -> dict[str, Any]:
    """Respuesta final de un ID de transacción, con el payload propuesto en la spec (M5).

    Única función que arma el payload. En NO_ACEPTADO (RN-01) el marketing va en null.
    """
    deleted = consent.is_deleted == 1
    return {
        "transaction_id": consent.transaction_id,
        "document_type": consent.document_type.value,
        "document_number": consent.document_number,
        "dealer": consent.dealer,
        "status": consent.status.value,
        "consents": {
            "personal_data": consent.consent_personal_data,
            "marketing_yamaha": None if deleted else consent.consent_marketing_yamaha,
            "marketing_dealer": None if deleted else consent.consent_marketing_dealer,
        },
        "responded_at": _iso_utc(consent.responded_at),
    }


def _iso_utc(value: datetime | None) -> str | None:
    # Mismo formato que las consultas de KIZUNA (sección 2.4): ISO 8601 en UTC con sufijo Z.
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
