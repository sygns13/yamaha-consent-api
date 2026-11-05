"""Validación de los datos ya normalizados (M2, sección 2.2).

Cada función recibe el valor normalizado y lo devuelve si es válido; si no, lanza ValueError con un mensaje en
español, que Pydantic convierte en un 422 con el detalle del campo.
"""

import re

from app.utils.normalization import PHONE_DIGITS

FULL_NAME_MAX_LENGTH = 250
DEALER_MAX_LENGTH = 100

CUSTOMER_ID_MAX_LENGTH = 20
DEALER_NAME_MAX_LENGTH = 150
EMAIL_MAX_LENGTH = 254

# customerId: alfanumérico = A–Z, Ñ y 0–9 (sin tildes ni otros alfabetos). El valor ya viene en mayúsculas.
_CUSTOMER_ID = re.compile(rf"[A-Z0-9Ñ]{{1,{CUSTOMER_ID_MAX_LENGTH}}}")
# Forma básica de un correo (algo@dominio.tld, sin espacios). No se valida más: un correo inválido no rechaza nada.
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def validate_customer_id(customer_id: str) -> str:
    if not _CUSTOMER_ID.fullmatch(customer_id):
        raise ValueError(
            "El identificador del cliente debe ser alfanumérico (letras, Ñ y dígitos), de máximo "
            f"{CUSTOMER_ID_MAX_LENGTH} caracteres"
        )
    return customer_id


def is_valid_email(email: str) -> bool:
    return len(email) <= EMAIL_MAX_LENGTH and _EMAIL.fullmatch(email) is not None


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
        raise ValueError("El código del dealer no puede estar vacío")
    if len(dealer) > DEALER_MAX_LENGTH:
        raise ValueError(f"El código del dealer no puede tener más de {DEALER_MAX_LENGTH} caracteres")
    return dealer
