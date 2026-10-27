"""Normalización de los datos que envía KIZUNA (M2, sección 2.2). Estas funciones no validan: ver validation.py."""

import re
import unicodedata

PHONE_DIGITS = 9

_DOCUMENT_SEPARATORS = re.compile(r"[\s-]+")
_NON_DIGITS = re.compile(r"[^0-9]")


def normalize_document_type(value: str) -> str:
    """Mayúsculas (y sin espacios alrededor): " dni " → "DNI"."""
    return value.strip().upper()


def normalize_document_number(value: str) -> str:
    """Quita espacios y guiones y pasa a mayúsculas: " 1234-5678 " → "12345678", "ab ñ123" → "ABÑ123".

    NFC: una "ñ" escrita como "n" + tilde combinada queda como un solo carácter "Ñ".
    """
    return unicodedata.normalize("NFC", _DOCUMENT_SEPARATORS.sub("", value).upper())


def normalize_full_name(value: str) -> str:
    """Trim, colapsa espacios múltiples y pasa a MAYÚSCULAS conservando tildes y Ñ.

    "  josé   ñuñez  " → "JOSÉ ÑUÑEZ". Se normaliza a NFC para que una tilde escrita como carácter combinado
    ("e" + "◌́") quede igual que "é" y no haya dos formas distintas del mismo nombre en la base.
    """
    return unicodedata.normalize("NFC", " ".join(value.split()).upper())


def normalize_phone(value: str, country_prefix: str) -> str:
    """Deja solo los dígitos y quita el prefijo de país si viene delante de un número de 9 dígitos.

    Con country_prefix="51": "+51 987-654-321" → "987654321", "(01) 98765" → "0198765" (inválido luego).
    Solo se aceptan dígitos ASCII: los de otros alfabetos se descartan como cualquier otro carácter.
    """
    digits = _NON_DIGITS.sub("", value)
    if len(digits) == len(country_prefix) + PHONE_DIGITS and digits.startswith(country_prefix):
        return digits[len(country_prefix) :]
    return digits


def normalize_dealer(value: str) -> str:
    """Trim. TODO(PENDIENTE-05): ¿KIZUNA envía código, nombre o ambos?"""
    return value.strip()
