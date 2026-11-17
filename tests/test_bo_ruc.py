"""Validación del RUC (BACKOFFICE_DEALERS.spec.md v1.2, Anexo B; fase B5, criterio B5.2).

Los mismos casos deben probarse en el frontend (`src/lib/validators/ruc.ts`).
"""

import pytest

from app.utils.ruc import (
    RUC_INVALID,
    RUC_LENGTH_INVALID,
    is_valid_ruc,
    normalize_ruc,
    ruc_check_digit,
    validate_ruc,
)


@pytest.mark.parametrize(
    "ruc",
    [
        # Ejemplos verificados del Anexo B.
        "20100047218",
        "20131312955",
        # r = 10 → dígito 0 y r = 11 → dígito 1.
        "20100000050",
        "20100000131",
        # Cada prefijo permitido.
        "10100000020",
        "10100000071",
    ],
)
def test_valid_rucs(ruc: str) -> None:
    assert is_valid_ruc(ruc)
    assert validate_ruc(ruc) == ruc


def test_check_digit_examples_of_annex_b() -> None:
    # suma 69 → 69 mod 11 = 3 → 11 − 3 = 8; suma 94 → 94 mod 11 = 6 → 11 − 6 = 5.
    assert ruc_check_digit("2010004721") == 8
    assert ruc_check_digit("2013131295") == 5
    assert ruc_check_digit("2010000005") == 0  # r = 10
    assert ruc_check_digit("2010000013") == 1  # r = 11


@pytest.mark.parametrize("prefix", ["10", "15", "16", "17", "20"])
def test_every_allowed_prefix(prefix: str) -> None:
    first_ten = f"{prefix}12345678"
    assert is_valid_ruc(f"{first_ten}{ruc_check_digit(first_ten)}")


@pytest.mark.parametrize(
    ("ruc", "message"),
    [
        ("20100047219", RUC_INVALID),  # dígito verificador errado (esperado 8)
        ("30100047218", RUC_INVALID),  # prefijo 30
        ("11100047218", RUC_INVALID),  # prefijo 11
        ("2010004721", RUC_LENGTH_INVALID),  # 10 dígitos
        ("201000472180", RUC_LENGTH_INVALID),  # 12 dígitos
        ("2010004721A", RUC_LENGTH_INVALID),  # letra
        ("20-10004721", RUC_LENGTH_INVALID),  # guion
        ("２０１０００４７２１８", RUC_LENGTH_INVALID),  # dígitos de ancho completo (no ASCII)
        ("", RUC_LENGTH_INVALID),
    ],
)
def test_invalid_rucs(ruc: str, message: str) -> None:
    assert not is_valid_ruc(ruc)
    with pytest.raises(ValueError, match=message):
        validate_ruc(ruc)


def test_messages_are_the_ones_of_annex_a3() -> None:
    assert RUC_LENGTH_INVALID == "El RUC debe tener 11 dígitos."
    assert RUC_INVALID == "El RUC no es válido."


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" 20100047218 ", "20100047218"), ("201 0004 7218", "20100047218"), ("20100047218\t", "20100047218")],
)
def test_normalize_removes_spaces(value: str, expected: str) -> None:
    assert normalize_ruc(value) == expected
