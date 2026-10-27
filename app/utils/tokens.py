"""Tokens de los enlaces de la landing. Solo se guarda el SHA-256: el token en claro nunca se persiste ni se loguea."""

import hashlib
import secrets

TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    """SHA-256 en hexadecimal (64 caracteres), lo que se guarda en consent_links.token_hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_link_token() -> tuple[str, str]:
    """Devuelve (token, token_hash). El token es seguro para URL: va en {LANDING_BASE_URL}/?t={token}."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)
