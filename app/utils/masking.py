"""Enmascarado de datos personales para logs. Nunca se escribe en logs un documento o teléfono en claro."""

VISIBLE_CHARS = 4
MASK_CHAR = "*"


def _mask_keep_last(value: str | None, visible: int = VISIBLE_CHARS) -> str | None:
    """Reemplaza por "*" todo menos los últimos `visible` caracteres, conservando la longitud.

    Si el valor tiene `visible` caracteres o menos, se enmascara completo (si no, quedaría en claro).
    """
    if value is None:
        return None
    value = value.strip()
    if len(value) <= visible:
        return MASK_CHAR * len(value)
    return MASK_CHAR * (len(value) - visible) + value[-visible:]


def mask_document(document_number: str | None) -> str | None:
    """Ejemplo: "12345678" → "****5678"."""
    return _mask_keep_last(document_number)


def mask_phone(phone: str | None) -> str | None:
    """Ejemplo: "987654321" → "*****4321"."""
    return _mask_keep_last(phone)
