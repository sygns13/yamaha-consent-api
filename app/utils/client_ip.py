"""IP del cliente para la evidencia del consentimiento (M4). Es un dato personal: nunca se loguea."""

import ipaddress


def client_ip(forwarded_for: str | None, fallback: str | None = None) -> str | None:
    """Primera IP de X-Forwarded-For; sin el header, `fallback` (la IP de la conexión, p. ej. en local).

    Devuelve la IP normalizada, o None si no es una IP válida (la columna es INET: un valor inválido haría fallar
    el registro del consentimiento).
    """
    candidate = forwarded_for.split(",", 1)[0] if forwarded_for else fallback
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        return None
