"""Contraseñas del BackOffice (BACKOFFICE_DEALERS.spec.md v1.2, secciones 5.1 y 5.2; criterios B2.9 y B2.13) y
validación de los campos de un usuario (sección 6.5, adelantada a B2 para la CLI)."""

from typing import Any

import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError

from app.core import passwords
from app.core.passwords import (
    PASSWORD_MAX_LENGTH,
    generate_temporary_password,
    hash_password,
    needs_rehash,
    policy_error_detail,
    policy_violations,
    temporary_password_length,
    verify_password,
)
from app.schemas.backoffice.users import UserCreateRequest
from app.utils.validation import USERNAME_INVALID

# --- B2.13 Hash ---


def test_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("Ñandú#2026")
    assert hashed.startswith("$argon2id$")
    assert "Ñandú#2026" not in hashed
    assert verify_password(hashed, "Ñandú#2026")
    assert not verify_password(hashed, "ñandú#2026")
    assert hash_password("Ñandú#2026") != hashed  # sal aleatoria


def test_invalid_hash_never_verifies() -> None:
    assert not verify_password("no-es-un-hash", "x")
    assert needs_rehash("no-es-un-hash")


def test_needs_rehash_when_parameters_change() -> None:
    assert not needs_rehash(hash_password("Clave#2026x"))
    assert needs_rehash(PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("Clave#2026x"))


def test_dummy_verification_uses_a_real_argon2_hash() -> None:
    assert passwords._DUMMY_HASH.startswith("$argon2id$")
    passwords.verify_dummy_password("cualquiera")  # no lanza


# --- Política (sección 5.1, v1.2) ---


@pytest.mark.parametrize(
    ("password", "violations"),
    [
        ("Ñandú#2026", []),
        ("Abc def#123", []),  # espacios intermedios permitidos
        ("Abcdefgh#1234567890x", []),  # 20 = máximo
        ("Abc#123", ["al menos 8 caracteres"]),
        ("Abcdefgh#1234567890xy", ["como máximo 20 caracteres"]),
        ("abcdef#123", ["una letra mayúscula"]),
        ("ABCDEF#123", ["una letra minúscula"]),
        ("Abcdefg#hi", ["un número"]),
        ("Abcdefg123", ["un carácter especial (por ejemplo ! @ # $ %)"]),
        ("Abcdefg١٢٣#", ["un número"]),  # solo 0–9 cuentan como número
        ("Abcdefg123²", ["un carácter especial (por ejemplo ! @ # $ %)"]),  # ² es un número, no especial
        ("Abcdefg123€", []),
        (" Abcdef#123", ["sin espacios al inicio ni al final"]),
        ("Abcdef#123\t", ["sin espacios al inicio ni al final"]),
        ("", ["al menos 8 caracteres", "una letra mayúscula", "una letra minúscula", "un número",
              "un carácter especial (por ejemplo ! @ # $ %)"]),
    ],
)  # fmt: skip
def test_policy_rules(password: str, violations: list[str]) -> None:
    assert policy_violations(password, min_length=8) == violations


@pytest.mark.parametrize("password", ["Ab#JPEREZ1", "Ab#jperez1", "Ab#1x45871236"])
def test_policy_rejects_username_or_dni_ignoring_case(password: str) -> None:
    assert policy_violations(password, min_length=8, username="jperez", dni="45871236") == [
        "no debe contener tu usuario ni tu DNI"
    ]


def test_policy_rejects_the_current_password() -> None:
    current = hash_password("Clave#2026x")
    assert policy_violations("Clave#2026x", min_length=8, current_hash=current) == [
        "distinta de la contraseña actual"
    ]
    assert policy_violations("Clave#2026y", min_length=8, current_hash=current) == []


def test_min_length_is_configurable() -> None:
    assert policy_violations("Abcdef#1234", min_length=12) == ["al menos 12 caracteres"]
    assert policy_violations("Abcdef#12345", min_length=12) == []


def test_policy_error_detail() -> None:
    assert policy_error_detail(["al menos 8 caracteres", "un número"]) == (
        "La contraseña no cumple los requisitos: al menos 8 caracteres, un número."
    )


# --- Contraseña temporal (sección 5.2, v1.2) ---


def test_temporary_password_length_is_max_of_20_and_min_length() -> None:
    assert temporary_password_length(8) == 20
    assert temporary_password_length(20) == 20
    assert temporary_password_length(25) == 25  # Settings no lo permite (8–20); la fórmula es la del spec


@pytest.mark.parametrize("min_length", [8, 14, 20])
def test_temporary_password(min_length: int) -> None:
    generated = {generate_temporary_password(min_length=min_length) for _ in range(200)}
    assert len(generated) == 200
    for password in generated:
        assert len(password) == PASSWORD_MAX_LENGTH == 20
        assert not set(password) & set("0Oo1lI")
        assert set(password) <= set("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%&*?")
        assert any(c.isupper() for c in password) and any(c.islower() for c in password)
        assert any(c.isdigit() for c in password) and any(c in "!@#$%&*?" for c in password)
        assert policy_violations(password, min_length=min_length) == []


def test_temporary_password_avoids_username_and_dni(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = iter(["Ab#2jperezxyzKMNPQRS", "Ab#22345678xyzKMNPQR", "Ab#2xyzKMNPQRSTUVWXy"])
    monkeypatch.setattr(passwords, "_random_temporary_password", lambda length: next(candidates))
    assert (
        generate_temporary_password(min_length=8, username="jperez", dni="22345678") == "Ab#2xyzKMNPQRSTUVWXy"
    )


def test_temporary_password_never_exceeds_the_maximum() -> None:
    with pytest.raises(ValueError, match="supera el máximo"):
        generate_temporary_password(min_length=21)


# --- Campos del usuario (sección 6.5, mensajes del Anexo A.3) ---

VALID_USER: dict[str, Any] = {
    "dni": "45871236",
    "firstNames": "Juan",
    "lastNames": "Pérez Gómez",
    "username": "jperez",
    "email": "jperez@dominio.com",
    "roleCode": "ADMIN",
}


def user_errors(**overrides: Any) -> dict[str, str]:
    with pytest.raises(ValidationError) as info:
        UserCreateRequest.model_validate({**VALID_USER, **overrides})
    return {str(e["loc"][0]): e["msg"].removeprefix("Value error, ") for e in info.value.errors()}


def test_valid_user_is_normalized() -> None:
    user = UserCreateRequest.model_validate(
        {
            **VALID_USER,
            "dni": " 45871236 ",
            "firstNames": "  maría   JOSÉ ",
            "lastNames": "O’Brien-Ñahui Jr.",
            "username": "  JPerez ",
            "email": " JPerez@Dominio.COM ",
            "phone": "+51 987-654-321",
        }
    )
    assert user.dni == "45871236"
    assert user.first_names == "maría JOSÉ"  # conserva mayúsculas y minúsculas
    assert user.last_names == "O’Brien-Ñahui Jr."
    assert user.username == "jperez"
    assert user.email == "jperez@dominio.com"
    assert user.phone == "987654321"
    assert user.role_code == "ADMIN"


def test_names_are_nfc() -> None:
    decomposed = "José"  # "e" + tilde combinada
    assert UserCreateRequest.model_validate({**VALID_USER, "firstNames": decomposed}).first_names == "José"


@pytest.mark.parametrize("phone", [None, "", "   "])
def test_phone_is_optional(phone: str | None) -> None:
    assert UserCreateRequest.model_validate({**VALID_USER, "phone": phone}).phone is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dni", "1234567", "El DNI debe tener 8 dígitos."),
        ("dni", "1234567a", "El DNI debe tener 8 dígitos."),
        ("dni", "１２３４５６７８", "El DNI debe tener 8 dígitos."),  # dígitos de ancho completo
        ("firstNames", "Juan2", "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion."),
        ("firstNames", "Juan_Carlos", "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion."),
        ("lastNames", "Иванов", "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion."),
        ("lastNames", ".-'", "El nombre solo puede contener letras, espacios, apóstrofo, punto y guion."),
        ("firstNames", "   ", "No puede estar vacío"),
        ("lastNames", "A" * 101, "Debe tener como máximo 100 caracteres"),
        ("username", "jp", USERNAME_INVALID),
        ("username", "1jperez", USERNAME_INVALID),
        ("username", "j perez", USERNAME_INVALID),
        ("username", "j" * 31, USERNAME_INVALID),
        ("email", "jperez", "El correo no tiene un formato válido."),
        ("email", "", "El correo no tiene un formato válido."),
        ("phone", "98765432", "El celular debe tener 9 dígitos."),
        ("phone", "+1 987 654 321", "El celular debe tener 9 dígitos."),
    ],
)  # fmt: skip
def test_invalid_user_fields(field: str, value: str, message: str) -> None:
    assert user_errors(**{field: value}) == {field: message}


def test_lastnames_accept_100_characters() -> None:
    assert UserCreateRequest.model_validate({**VALID_USER, "lastNames": "Á" * 100}).last_names == "Á" * 100


def test_unknown_role_and_extra_fields_are_rejected() -> None:
    errors = user_errors(roleCode="SUPERVISOR", password="x")
    assert set(errors) == {"roleCode", "password"}
