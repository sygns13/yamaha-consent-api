import pytest

from app.utils.masking import mask_document, mask_phone


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12345678", "****5678"),  # DNI (ejemplo de la spec)
        ("20123456789", "*******6789"),  # RUC
        ("AB1234567", "*****4567"),  # CE / PASAPORTE alfanumérico
        ("12345", "*2345"),
        ("  12345678  ", "****5678"),  # ignora espacios alrededor
    ],
)
def test_mask_document(value: str, expected: str) -> None:
    assert mask_document(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("987654321", "*****4321"),  # ejemplo de la spec
        ("whatsapp:+51987654321", "*****************4321"),
    ],
)
def test_mask_phone(value: str, expected: str) -> None:
    assert mask_phone(value) == expected


@pytest.mark.parametrize("mask", [mask_document, mask_phone])
@pytest.mark.parametrize(("value", "expected"), [("1234", "****"), ("12", "**"), ("", "")])
def test_short_values_are_fully_masked(mask, value: str, expected: str) -> None:
    # Con 4 caracteres o menos, mostrar los "últimos 4" dejaría el valor en claro.
    assert mask(value) == expected


@pytest.mark.parametrize("mask", [mask_document, mask_phone])
def test_none_returns_none(mask) -> None:
    assert mask(None) is None


@pytest.mark.parametrize("mask", [mask_document, mask_phone])
def test_mask_keeps_length_and_hides_prefix(mask) -> None:
    value = "123456789012"
    masked = mask(value)
    assert len(masked) == len(value)
    assert masked[-4:] == value[-4:]
    assert set(masked[:-4]) == {"*"}
