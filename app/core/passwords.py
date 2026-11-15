"""Contraseñas del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, secciones 5.1 y 5.2).

- Hash Argon2id con los parámetros por defecto de `argon2-cffi` (perfil RFC 9106). Solo se compara con
  `PasswordHasher.verify` (tiempo constante).
- Política: largo BACKOFFICE_PASSWORD_MIN_LENGTH–20, mayúscula, minúscula, número, carácter especial, sin espacios al
  inicio ni al final, sin el usuario ni el DNI, distinta de la actual. El servidor es la única validación que manda.
- Contraseña temporal generada con `secrets`.

Nunca se loguea una contraseña ni un hash.
"""

import secrets
import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

PASSWORD_MAX_LENGTH = 20
TEMPORARY_PASSWORD_MIN_LENGTH = 20

# Contraseña temporal: sin caracteres ambiguos (0 O o 1 l I).
_AMBIGUOUS = frozenset("0Oo1lI")
_TEMP_UPPER = "".join(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in _AMBIGUOUS)
_TEMP_LOWER = "".join(c for c in "abcdefghijklmnopqrstuvwxyz" if c not in _AMBIGUOUS)
_TEMP_DIGITS = "".join(c for c in "0123456789" if c not in _AMBIGUOUS)
_TEMP_SYMBOLS = "!@#$%&*?"
_TEMP_ALPHABET = _TEMP_UPPER + _TEMP_LOWER + _TEMP_DIGITS + _TEMP_SYMBOLS

# Textos de cada requisito incumplido (Anexo A.3), en el orden en que se enumeran.
POLICY_ERROR_PREFIX = "La contraseña no cumple los requisitos: "
REQ_MIN_LENGTH = "al menos {n} caracteres"
REQ_MAX_LENGTH = "como máximo {n} caracteres"
REQ_UPPERCASE = "una letra mayúscula"
REQ_LOWERCASE = "una letra minúscula"
REQ_DIGIT = "un número"
REQ_SPECIAL = "un carácter especial (por ejemplo ! @ # $ %)"
REQ_NO_EDGE_SPACES = "sin espacios al inicio ni al final"
REQ_NO_PERSONAL_DATA = "no debe contener tu usuario ni tu DNI"
REQ_NOT_CURRENT = "distinta de la contraseña actual"

_hasher = PasswordHasher()
# Hash ficticio precalculado: un login con un usuario inexistente verifica contra él, para que la respuesta tarde lo
# mismo que con un usuario existente (sección 5.1).
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    """Hash Argon2id en formato PHC (`$argon2id$...`)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True si la contraseña corresponde al hash. Un hash con formato inválido cuenta como no coincidente."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def verify_dummy_password(password: str) -> None:
    """Verificación contra el hash ficticio (usuario inexistente): solo iguala el tiempo de respuesta."""
    verify_password(_DUMMY_HASH, password)


def needs_rehash(password_hash: str) -> bool:
    """True si el hash se calculó con parámetros distintos a los actuales (se recalcula en el siguiente login)."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True)
class PasswordPolicy:
    """Lo que expone GET /auth/password-policy (el frontend arma el checklist con esto)."""

    min_length: int
    max_length: int = PASSWORD_MAX_LENGTH
    require_uppercase: bool = True
    require_lowercase: bool = True
    require_digit: bool = True
    require_special: bool = True


def _is_special(char: str) -> bool:
    # Cualquier carácter que no sea letra, número ni espacio (frontend: [^\p{L}\p{N}\s]).
    return unicodedata.category(char)[0] not in ("L", "N") and not char.isspace()


def policy_violations(
    password: str,
    *,
    min_length: int,
    username: str | None = None,
    dni: str | None = None,
    current_hash: str | None = None,
) -> list[str]:
    """Requisitos incumplidos (textos del Anexo A.3, en orden). Lista vacía = cumple la política.

    `current_hash` solo se pasa al cambiar una contraseña existente (no puede ser igual a la actual).
    """
    violations: list[str] = []
    if len(password) < min_length:
        violations.append(REQ_MIN_LENGTH.format(n=min_length))
    if len(password) > PASSWORD_MAX_LENGTH:
        violations.append(REQ_MAX_LENGTH.format(n=PASSWORD_MAX_LENGTH))
    if not any(unicodedata.category(c) == "Lu" for c in password):
        violations.append(REQ_UPPERCASE)
    if not any(unicodedata.category(c) == "Ll" for c in password):
        violations.append(REQ_LOWERCASE)
    if not any(c in "0123456789" for c in password):
        violations.append(REQ_DIGIT)
    if not any(_is_special(c) for c in password):
        violations.append(REQ_SPECIAL)
    if password != password.strip():
        violations.append(REQ_NO_EDGE_SPACES)
    folded = password.casefold()
    if any(value and value.casefold() in folded for value in (username, dni)):
        violations.append(REQ_NO_PERSONAL_DATA)
    if current_hash is not None and verify_password(current_hash, password):
        violations.append(REQ_NOT_CURRENT)
    return violations


def policy_error_detail(violations: list[str]) -> str:
    """ "La contraseña no cumple los requisitos: al menos 8 caracteres, un número." (Anexo A.3)."""
    return f"{POLICY_ERROR_PREFIX}{', '.join(violations)}."


def temporary_password_length(min_length: int) -> int:
    """max(20, BACKOFFICE_PASSWORD_MIN_LENGTH) (sección 5.2, v1.2)."""
    return max(TEMPORARY_PASSWORD_MIN_LENGTH, min_length)


def _random_temporary_password(length: int) -> str:
    rng = secrets.SystemRandom()
    chars = [
        secrets.choice(_TEMP_UPPER),
        secrets.choice(_TEMP_LOWER),
        secrets.choice(_TEMP_DIGITS),
        secrets.choice(_TEMP_SYMBOLS),
    ]
    chars += [secrets.choice(_TEMP_ALPHABET) for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def generate_temporary_password(
    *, min_length: int, username: str | None = None, dni: str | None = None
) -> str:
    """Contraseña temporal que cumple la política para ese usuario (sección 5.2). Se muestra una sola vez y nunca se
    guarda en claro ni se loguea."""
    length = temporary_password_length(min_length)
    if length > PASSWORD_MAX_LENGTH:  # Settings limita el mínimo a 8–20: no debería ocurrir
        raise ValueError("BACKOFFICE_PASSWORD_MIN_LENGTH supera el máximo de la política")
    while True:
        password = _random_temporary_password(length)
        if not policy_violations(password, min_length=min_length, username=username, dni=dni):
            return password
