"""Validación del RUC (BACKOFFICE_DEALERS.spec.md, Anexo B).

1. Se quitan los espacios; debe tener exactamente 11 dígitos.
2. Prefijo 10, 15, 16, 17 o 20.
3. Dígito verificador: pesos [5, 4, 3, 2, 7, 6, 5, 4, 3, 2] sobre los 10 primeros dígitos; r = 11 − (suma mod 11);
   r = 10 → 0, r = 11 → 1. El undécimo dígito debe ser r.

El RUC no es único entre Dealers (varias sedes lo comparten). El frontend tiene la misma función
(`src/lib/validators/ruc.ts`) con los mismos casos de prueba.
"""

RUC_LENGTH = 11
RUC_PREFIXES = frozenset({"10", "15", "16", "17", "20"})
_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)

# Mensajes del Anexo A.3.
RUC_LENGTH_INVALID = "El RUC debe tener 11 dígitos."
RUC_INVALID = "El RUC no es válido."


def normalize_ruc(value: str) -> str:
    """Quita los espacios (" 20100047218 " y "201 0004 7218" → "20100047218")."""
    return "".join(value.split())


def ruc_check_digit(first_ten: str) -> int:
    """Dígito verificador de los 10 primeros dígitos."""
    remainder = 11 - sum(int(digit) * weight for digit, weight in zip(first_ten, _WEIGHTS, strict=True)) % 11
    return {10: 0, 11: 1}.get(remainder, remainder)


def _has_ruc_shape(ruc: str) -> bool:
    # Solo dígitos ASCII: str.isdigit() acepta también dígitos de otros alfabetos.
    return len(ruc) == RUC_LENGTH and ruc.isascii() and ruc.isdigit()


def is_valid_ruc(ruc: str) -> bool:
    """`ruc` ya normalizado."""
    return _has_ruc_shape(ruc) and ruc[:2] in RUC_PREFIXES and ruc_check_digit(ruc[:10]) == int(ruc[10])


def validate_ruc(ruc: str) -> str:
    """`ruc` ya normalizado. ValueError con el mensaje del Anexo A.3 (Pydantic lo convierte en un 422)."""
    if not _has_ruc_shape(ruc):
        raise ValueError(RUC_LENGTH_INVALID)
    if not is_valid_ruc(ruc):
        raise ValueError(RUC_INVALID)
    return ruc
