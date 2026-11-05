import unicodedata

import pytest

from app.utils.normalization import (
    normalize_customer_id,
    normalize_dealer,
    normalize_dealer_name,
    normalize_email,
    normalize_full_name,
    normalize_phone,
)
from app.utils.validation import (
    is_valid_email,
    validate_customer_id,
    validate_dealer,
    validate_full_name,
    validate_phone,
)

# --- customerId ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12345678", "12345678"),
        ("  12345678  ", "12345678"),
        ("1234-5678", "12345678"),
        ("12 34 56 78", "12345678"),
        ("20-12345678-9", "20123456789"),
        ("ab-123 cd", "AB123CD"),
        ("1234\t5678\n", "12345678"),  # tabs y saltos de línea también son espacios
        ("1234 5678", "12345678"),  # espacio no separable
        ("---", ""),
        ("peña-123", "PEÑA123"),  # ñ minúscula → Ñ
    ],
)
def test_normalize_customer_id(value: str, expected: str) -> None:
    assert normalize_customer_id(value) == expected


def test_normalize_customer_id_composes_decomposed_enie() -> None:
    decomposed = unicodedata.normalize("NFD", "añ123")  # "n" + tilde combinada: 6 caracteres
    assert len(decomposed) == 6
    assert normalize_customer_id(decomposed) == "AÑ123"


@pytest.mark.parametrize(
    "value",
    [
        "12345678",
        "01234567",
        "20123456789",
        "A",
        "AB1234567890",
        "PEÑA12345",
        "Ñ" * 20,
        "ABCDEFGHIJ0123456789",
    ],
)
def test_valid_customer_ids(value: str) -> None:
    assert validate_customer_id(value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "alfanumérico"),
        ("A" * 21, "máximo 20"),
        ("AÉ123", "alfanumérico"),  # tildes: no
        ("AÇ123", "alfanumérico"),
        ("ΑΒΓ123", "alfanumérico"),  # letras griegas
        ("X123.456", "alfanumérico"),
        ("AB/123", "alfanumérico"),
        ("١٢٣٤٥٦٧٨", "alfanumérico"),  # dígitos arábigos: no son ASCII
    ],
)
def test_invalid_customer_ids(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_customer_id(value)


# --- customerEmail y dealerName ---


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), ("", None), ("   ", None), (" A@B.Com ", "a@b.com")]
)
def test_normalize_email(value: str | None, expected: str | None) -> None:
    assert normalize_email(value) == expected


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a@b.com", True),
        ("nombre.apellido@mail.com.pe", True),
        ("a@b", False),
        ("ab.com", False),
        ("a b@c.com", False),
        ("a@@b.com", False),
        ("x" * 250 + "@a.com", False),
    ],
)
def test_is_valid_email(value: str, valid: bool) -> None:
    assert is_valid_email(value) is valid


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), ("  ", None), ("  ABC   Motors ", "ABC Motors"), ("abc", "abc")]
)
def test_normalize_dealer_name(value: str | None, expected: str | None) -> None:
    assert normalize_dealer_name(value) == expected


# --- full_name ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("juan  pérez   gómez", "JUAN PÉREZ GÓMEZ"),  # ejemplo de la spec
        ("  josé ñuñez  ", "JOSÉ ÑUÑEZ"),
        ("MARÍA ÁNGELES", "MARÍA ÁNGELES"),
        ("raúl ibáñez", "RAÚL IBÁÑEZ"),
        ("ñandú", "ÑANDÚ"),
        ("güemes müller", "GÜEMES MÜLLER"),
        ("ana\tlucía\nrojas", "ANA LUCÍA ROJAS"),  # tabs y saltos de línea
        ("ana rojas", "ANA ROJAS"),  # espacio no separable
        ("o'higgins d'angelo-ruiz", "O'HIGGINS D'ANGELO-RUIZ"),  # apóstrofos y guiones se conservan
        ("   ", ""),
    ],
)
def test_normalize_full_name(value: str, expected: str) -> None:
    assert normalize_full_name(value) == expected


def test_normalize_full_name_composes_decomposed_accents() -> None:
    decomposed = unicodedata.normalize("NFD", "josé ñuñez")
    assert decomposed != "josé ñuñez"
    result = normalize_full_name(decomposed)
    assert result == "JOSÉ ÑUÑEZ"
    assert len(result) == len("JOSÉ ÑUÑEZ")


def test_validate_full_name() -> None:
    assert validate_full_name("A" * 250) == "A" * 250
    with pytest.raises(ValueError, match="vacío"):
        validate_full_name("")
    with pytest.raises(ValueError, match="250"):
        validate_full_name("A" * 251)


# --- phone ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+51 987-654-321", "987654321"),  # caso de la spec
        ("+51 987 654 321", "987654321"),
        ("51987654321", "987654321"),
        ("(+51) 987.654.321", "987654321"),
        ("987654321", "987654321"),
        ("987 654 321", "987654321"),
        ("519876543", "519876543"),  # 9 dígitos que empiezan con 51: no es prefijo
        ("52987654321", "52987654321"),  # 11 dígitos con otro prefijo: no se toca
        ("0051987654321", "0051987654321"),  # 13 dígitos: no se toca
        ("5198765432", "5198765432"),  # 10 dígitos: no se toca
        ("٩٨٧٦٥٤٣٢١", ""),  # dígitos arábigos: se descartan
        ("abc", ""),
    ],
)
def test_normalize_phone(value: str, expected: str) -> None:
    assert normalize_phone(value, "51") == expected


def test_normalize_phone_uses_configured_prefix() -> None:
    assert normalize_phone("+1 987 654 321", "1") == "987654321"
    assert normalize_phone("+51 987 654 321", "1") == "51987654321"


@pytest.mark.parametrize("value", ["987654321", "012345678"])
def test_valid_phone(value: str) -> None:
    assert validate_phone(value) == value


@pytest.mark.parametrize("value", ["", "98765432", "9876543210", "51987654321", "98765432a", "٩٨٧٦٥٤٣٢١"])
def test_invalid_phone(value: str) -> None:
    with pytest.raises(ValueError, match="9 dígitos"):
        validate_phone(value)


# --- dealer ---


def test_dealer() -> None:
    assert normalize_dealer("  DEALER-001 ") == "DEALER-001"
    # Solo trim: no cambia mayúsculas ni espacios internos.
    assert normalize_dealer("dealer 001") == "dealer 001"
    assert validate_dealer("D" * 100) == "D" * 100
    with pytest.raises(ValueError, match="vacío"):
        validate_dealer("")
    with pytest.raises(ValueError, match="100"):
        validate_dealer("D" * 101)
