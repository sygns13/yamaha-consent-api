"""Tokens opacos: enlaces de la landing, sesiones y enlaces de restablecimiento del BackOffice.

Solo se guarda el SHA-256: el token en claro nunca se persiste ni se loguea.
"""

import hashlib
import secrets

TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    """SHA-256 en hexadecimal (64 caracteres): consent_links, user_sessions y password_reset_tokens.token_hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str]:
    """Devuelve (token, token_hash). `secrets.token_urlsafe(32)`: seguro para URL y para una cookie."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def generate_link_token() -> tuple[str, str]:
    """Token del enlace de la landing: va en {LANDING_BASE_URL}/?t={token}."""
    return generate_token()
