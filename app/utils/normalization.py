"""Normalización de los datos que envía KIZUNA (M2, sección 2.2). Estas funciones no validan: ver validation.py."""

import re
import unicodedata

PHONE_DIGITS = 9

_CUSTOMER_ID_SEPARATORS = re.compile(r"[\s-]+")
_NON_DIGITS = re.compile(r"[^0-9]")


def normalize_customer_id(value: str) -> str:
    """Quita espacios y guiones y pasa a mayúsculas: " 7629-9887 " → "76299887", "ab ñ123" → "ABÑ123".

    NFC: una "ñ" escrita como "n" + tilde combinada queda como un solo carácter "Ñ".
    """
    return unicodedata.normalize("NFC", _CUSTOMER_ID_SEPARATORS.sub("", value).upper())


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
    """Trim (dealerCode)."""
    return value.strip()


def normalize_dealer_name(value: str | None) -> str | None:
    """Trim y espacios múltiples colapsados; vacío → None. Se conservan mayúsculas y minúsculas (se muestra)."""
    if value is None:
        return None
    return " ".join(value.split()) or None


def normalize_email(value: str | None) -> str | None:
    """Trim y minúsculas; vacío → None."""
    if value is None:
        return None
    return value.strip().lower() or None
