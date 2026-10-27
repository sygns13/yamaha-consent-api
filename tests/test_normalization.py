import unicodedata

import pytest

from app.models.enums import DocumentType
from app.utils.normalization import (
    normalize_dealer,
    normalize_document_number,
    normalize_document_type,
    normalize_full_name,
    normalize_phone,
)
from app.utils.validation import (
    validate_dealer,
    validate_document_number,
    validate_full_name,
    validate_phone,
)

# --- document_type ---


@pytest.mark.parametrize(("value", "expected"), [("dni", "DNI"), (" Pasaporte ", "PASAPORTE"), ("CE", "CE")])
def test_normalize_document_type(value: str, expected: str) -> None:
    assert normalize_document_type(value) == expected


# --- document_number ---


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
def test_normalize_document_number(value: str, expected: str) -> None:
    assert normalize_document_number(value) == expected


def test_normalize_document_number_composes_decomposed_enie() -> None:
    decomposed = unicodedata.normalize("NFD", "añ123")  # "n" + tilde combinada: 6 caracteres
    assert len(decomposed) == 6
    assert normalize_document_number(decomposed) == "AÑ123"


@pytest.mark.parametrize(
    ("document_type", "value"),
    [
        (DocumentType.DNI, "12345678"),
        (DocumentType.DNI, "01234567"),  # ceros a la izquierda
        (DocumentType.RUC, "20123456789"),
        (DocumentType.CE, "A"),
        (DocumentType.CE, "AB1234567890"),  # 12
        (DocumentType.PASAPORTE, "X12345678"),
        (DocumentType.PASAPORTE, "123456789012"),  # 12, solo dígitos también es alfanumérico
        (DocumentType.OTROS, "ABCDEFGHIJ0123456789"),  # 20
        (DocumentType.CE, "AÑ123"),  # la Ñ cuenta como alfanumérica
        (DocumentType.CE, "ÑÑÑÑÑÑÑÑÑÑÑÑ"),  # 12 Ñ
        (DocumentType.PASAPORTE, "PEÑA12345"),
        (DocumentType.OTROS, "Ñ" * 20),
    ],
)
def test_valid_document_numbers(document_type: DocumentType, value: str) -> None:
    assert validate_document_number(document_type, value) == value


@pytest.mark.parametrize(
    ("document_type", "value", "message"),
    [
        (DocumentType.DNI, "1234567", "8 dígitos"),  # 7
        (DocumentType.DNI, "123456789", "8 dígitos"),  # 9
        (DocumentType.DNI, "1234567A", "8 dígitos"),  # letra
        (DocumentType.DNI, "", "8 dígitos"),
        (DocumentType.DNI, "١٢٣٤٥٦٧٨", "8 dígitos"),  # dígitos arábigos: no son ASCII
        (DocumentType.RUC, "2012345678", "11 dígitos"),  # 10
        (DocumentType.RUC, "201234567890", "11 dígitos"),  # 12
        (DocumentType.RUC, "2012345678A", "11 dígitos"),
        (DocumentType.CE, "AB12345678901", "máximo 12"),  # 13
        (DocumentType.CE, "", "máximo 12"),
        (DocumentType.CE, "AÉ123", "alfanumérico"),  # tildes: no
        (DocumentType.CE, "AÇ123", "alfanumérico"),
        (DocumentType.CE, "Ñ" * 13, "máximo 12"),
        (DocumentType.DNI, "1234567Ñ", "8 dígitos"),  # la Ñ no vale en DNI ni RUC
        (DocumentType.PASAPORTE, "X1234567890AB", "máximo 12"),  # 13
        (DocumentType.PASAPORTE, "ΑΒΓ123", "alfanumérico"),  # letras griegas
        (DocumentType.PASAPORTE, "X123.456", "alfanumérico"),
        (DocumentType.OTROS, "A" * 21, "máximo 20"),
        (DocumentType.OTROS, "AB/123", "alfanumérico"),
    ],
)
def test_invalid_document_numbers(document_type: DocumentType, value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_document_number(document_type, value)


@pytest.mark.parametrize("document_type", list(DocumentType))
def test_every_document_type_has_a_rule(document_type: DocumentType) -> None:
    # Si se agrega un tipo al enum sin regla, validate_document_number lanza KeyError.
    with pytest.raises(ValueError):
        validate_document_number(document_type, "")


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
