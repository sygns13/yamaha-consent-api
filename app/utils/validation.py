"""Validación de los datos ya normalizados (M2, sección 2.2).

Cada función recibe el valor normalizado y lo devuelve si es válido; si no, lanza ValueError con un mensaje en
español, que Pydantic convierte en un 422 con el detalle del campo.
"""

import re
import unicodedata

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


# --- Usuarios del BackOffice (BACKOFFICE_DEALERS.spec.md, secciones 4.3 y 6.5; mensajes del Anexo A.3) ---

PERSON_NAME_MAX_LENGTH = 100
DNI_INVALID = "El DNI debe tener 8 dígitos."
PERSON_NAME_INVALID = "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion."
PERSON_NAME_EMPTY = "No puede estar vacío"
PERSON_NAME_TOO_LONG = f"Debe tener como máximo {PERSON_NAME_MAX_LENGTH} caracteres"
USERNAME_INVALID = (
    "El usuario debe tener de 4 a 30 caracteres: letras minúsculas, números, punto, guion o guion bajo, y "
    "empezar con una letra."
)
USER_EMAIL_INVALID = "El correo no tiene un formato válido."
MOBILE_INVALID = "El celular debe tener 9 dígitos."

_DNI = re.compile(r"[0-9]{8}")
# Igual que el CHECK users.username_format.
_USERNAME = re.compile(r"[a-z][a-z0-9._-]{3,29}")
# Además de letras: espacio, apóstrofo (recto o tipográfico), punto y guion.
_PERSON_NAME_SYMBOLS = frozenset(" '\u2019.-")


def validate_dni(dni: str) -> str:
    if not _DNI.fullmatch(dni):
        raise ValueError(DNI_INVALID)
    return dni


def _is_latin_letter(char: str) -> bool:
    # Letras del alfabeto latino, con tildes, diéresis y Ñ (no otros alfabetos).
    return unicodedata.category(char).startswith("L") and unicodedata.name(char, "").startswith("LATIN ")


def validate_person_name(name: str) -> str:
    """Nombres o apellidos ya normalizados (normalize_person_name): 1–100 caracteres, letras, espacios, apóstrofo,
    punto y guion, con al menos una letra."""
    if not name:
        raise ValueError(PERSON_NAME_EMPTY)
    if len(name) > PERSON_NAME_MAX_LENGTH:
        raise ValueError(PERSON_NAME_TOO_LONG)
    if not all(_is_latin_letter(c) or c in _PERSON_NAME_SYMBOLS for c in name) or not any(
        _is_latin_letter(c) for c in name
    ):
        raise ValueError(PERSON_NAME_INVALID)
    return name


def validate_username(username: str) -> str:
    if not _USERNAME.fullmatch(username):
        raise ValueError(USERNAME_INVALID)
    return username


def validate_user_email(email: str) -> str:
    """A diferencia de customerEmail (KIZUNA), el correo de un usuario es obligatorio y un formato inválido rechaza."""
    if not is_valid_email(email):
        raise ValueError(USER_EMAIL_INVALID)
    return email


def validate_mobile(phone: str) -> str:
    """Celular de un usuario del BackOffice, ya normalizado con normalize_phone: 9 dígitos."""
    if len(phone) != PHONE_DIGITS or not phone.isascii() or not phone.isdigit():
        raise ValueError(MOBILE_INVALID)
    return phone
