"""Validación de los datos ya normalizados (M2, sección 2.2).

Cada función recibe el valor normalizado y lo devuelve si es válido; si no, lanza ValueError con un mensaje en
español, que Pydantic convierte en un 422 con el detalle del campo.
"""

import re

from app.models.enums import DocumentType
from app.utils.normalization import PHONE_DIGITS

FULL_NAME_MAX_LENGTH = 250
DEALER_MAX_LENGTH = 100

# Reglas por tipo de documento. CE, PASAPORTE y OTROS: alfanumérico = A–Z, Ñ y 0–9 (sin tildes ni otros
# alfabetos). El valor ya viene en mayúsculas. OTROS es (propuesta).
_ALPHANUMERIC = "A-Z0-9Ñ"
_DOCUMENT_RULES: dict[DocumentType, tuple[re.Pattern[str], str]] = {
    DocumentType.DNI: (re.compile(r"[0-9]{8}"), "El DNI debe tener exactamente 8 dígitos"),
    DocumentType.RUC: (re.compile(r"[0-9]{11}"), "El RUC debe tener exactamente 11 dígitos"),
    DocumentType.CE: (
        re.compile(rf"[{_ALPHANUMERIC}]{{1,12}}"),
        "El carné de extranjería debe ser alfanumérico (letras, Ñ y dígitos), de máximo 12 caracteres",
    ),
    DocumentType.PASAPORTE: (
        re.compile(rf"[{_ALPHANUMERIC}]{{1,12}}"),
        "El pasaporte debe ser alfanumérico (letras, Ñ y dígitos), de máximo 12 caracteres",
    ),
    DocumentType.OTROS: (
        re.compile(rf"[{_ALPHANUMERIC}]{{1,20}}"),
        "El documento debe ser alfanumérico (letras, Ñ y dígitos), de máximo 20 caracteres",
    ),
}


def validate_document_number(document_type: DocumentType, document_number: str) -> str:
    pattern, message = _DOCUMENT_RULES[document_type]
    if not pattern.fullmatch(document_number):
        raise ValueError(message)
    return document_number


def validate_full_name(full_name: str) -> str:
    if not full_name:
        raise ValueError("El nombre no puede estar vacío")
    if len(full_name) > FULL_NAME_MAX_LENGTH:
        raise ValueError(f"El nombre no puede tener más de {FULL_NAME_MAX_LENGTH} caracteres")
    return full_name


def validate_phone(phone: str) -> str:
    # TODO(PENDIENTE-10): los números extranjeros no cumplen la regla de 9 dígitos.
    if len(phone) != PHONE_DIGITS or not phone.isascii() or not phone.isdigit():
        raise ValueError(f"El teléfono debe tener exactamente {PHONE_DIGITS} dígitos (formato XXXXXXXXX)")
    return phone


def validate_dealer(dealer: str) -> str:
    if not dealer:
        raise ValueError("El dealer no puede estar vacío")
    if len(dealer) > DEALER_MAX_LENGTH:
        raise ValueError(f"El dealer no puede tener más de {DEALER_MAX_LENGTH} caracteres")
    return dealer
