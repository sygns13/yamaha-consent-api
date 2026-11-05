"""Fechas que se intercambian con KIZUNA: ISO 8601 con el offset de su zona horaria (KIZUNA_TIMEZONE)."""

from datetime import datetime
from zoneinfo import ZoneInfo


def to_kizuna_iso(value: datetime | None, zone: ZoneInfo) -> str | None:
    """Ejemplo con America/Lima: 2026-11-02 18:35:00 UTC → "2026-11-02T13:35:00-05:00". Sin fracciones de segundo."""
    if value is None:
        return None
    return value.astimezone(zone).isoformat(timespec="seconds")
