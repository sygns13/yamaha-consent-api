from typing import Any

import pytest
from pydantic import ValidationError

from app.core.validation_errors import translate_errors
from app.models.enums import DocumentType
from app.schemas import ConsentCreateRequest, ConsentListQuery

VALID = {
    "transaction_id": "TRX-000123",
    "document_type": "DNI",
    "document_number": "12345678",
    "full_name": "juan  pérez   gómez",
    "phone": "+51 987 654 321",
    "dealer": "DEALER-001",
    "additional_info": {},
}


def errors_by_field(data: dict[str, Any], model: type = ConsentCreateRequest) -> dict[str, str]:
    """Errores {campo: mensaje en español}, tal como los recibe KIZUNA en el 422."""
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    return {".".join(str(p) for p in err["loc"]): err["msg"] for err in translate_errors(exc.value.errors())}


def test_spec_example_is_normalized() -> None:
    request = ConsentCreateRequest.model_validate(VALID)
    assert request.transaction_id == "TRX-000123"
    assert request.document_type is DocumentType.DNI
    assert request.document_number == "12345678"
    assert request.full_name == "JUAN PÉREZ GÓMEZ"
    assert request.phone == "987654321"
    assert request.dealer == "DEALER-001"
    assert request.additional_info == {}


def test_normalizes_every_field() -> None:
    request = ConsentCreateRequest.model_validate(
        {
            **VALID,
            "document_type": " ce ",
            "document_number": " ab-1234 5678 ",
            "full_name": "  josé   ñuñez  ",
            "phone": "+51 987-654-321",
            "dealer": "  DEALER-001  ",
        }
    )
    assert request.document_type is DocumentType.CE
    assert request.document_number == "AB12345678"
    assert request.full_name == "JOSÉ ÑUÑEZ"
    assert request.phone == "987654321"
    assert request.dealer == "DEALER-001"


def test_additional_info_is_optional() -> None:
    data = {k: v for k, v in VALID.items() if k != "additional_info"}
    assert ConsentCreateRequest.model_validate(data).additional_info is None


def test_additional_info_keeps_nested_json() -> None:
    info = {"canal": "tienda", "items": [1, 2], "meta": {"a": None}}
    assert ConsentCreateRequest.model_validate({**VALID, "additional_info": info}).additional_info == info


def test_transaction_id_is_kept_as_sent() -> None:
    # Es la PK que KIZUNA usa luego para consultar: no se modifica.
    request = ConsentCreateRequest.model_validate({**VALID, "transaction_id": "trx-00 1"})
    assert request.transaction_id == "trx-00 1"


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"document_number": "1234567"}, "document_number", "8 dígitos"),
        ({"document_number": "1234567A"}, "document_number", "8 dígitos"),
        ({"document_type": "RUC", "document_number": "12345678"}, "document_number", "11 dígitos"),
        ({"document_type": "CE", "document_number": "AB12345678901"}, "document_number", "máximo 12"),
        ({"document_type": "PASAPORTE", "document_number": "X.1"}, "document_number", "alfanumérico"),
        ({"document_type": "OTROS", "document_number": "A" * 21}, "document_number", "máximo 20"),
        ({"document_type": "CE", "document_number": "AÉ123"}, "document_number", "alfanumérico"),
        (
            {"document_type": "LIBRETA"},
            "document_type",
            "Debe ser uno de estos valores: 'DNI', 'RUC', 'CE', 'PASAPORTE' o 'OTROS'",
        ),
        ({"full_name": "   "}, "full_name", "vacío"),
        ({"full_name": "A" * 251}, "full_name", "250"),
        ({"phone": "987 654 32"}, "phone", "9 dígitos"),
        ({"phone": "+52 987 654 321"}, "phone", "9 dígitos"),
        ({"phone": "0051 987 654 321"}, "phone", "9 dígitos"),
        ({"dealer": "   "}, "dealer", "vacío"),
        ({"dealer": "D" * 101}, "dealer", "100"),
        ({"transaction_id": ""}, "transaction_id", "No puede estar vacío"),
        ({"transaction_id": "   "}, "transaction_id", "El ID de transacción no puede estar vacío"),
        ({"transaction_id": "T" * 101}, "transaction_id", "Debe tener como máximo 100 caracteres"),
        ({"additional_info": [1, 2]}, "additional_info", "Debe ser un objeto JSON"),
        ({"additional_info": "texto"}, "additional_info", "Debe ser un objeto JSON"),
    ],
)
def test_invalid_field_reports_that_field(overrides: dict[str, Any], field: str, message: str) -> None:
    errors = errors_by_field({**VALID, **overrides})
    assert list(errors) == [field]
    assert message in errors[field]
    assert not errors[field].startswith("Value error")


def test_own_validator_messages_are_returned_as_is() -> None:
    errors = errors_by_field({**VALID, "document_number": "1234567"})
    assert errors == {"document_number": "El DNI debe tener exactamente 8 dígitos"}


def test_document_with_enie_is_accepted() -> None:
    request = ConsentCreateRequest.model_validate(
        {**VALID, "document_type": "pasaporte", "document_number": "peña-1234"}
    )
    assert request.document_number == "PEÑA1234"


@pytest.mark.parametrize("field", ["document_number", "phone"])
def test_numbers_must_be_strings(field: str) -> None:
    # Un número JSON perdería los ceros a la izquierda (DNI 01234567): se exige texto.
    errors = errors_by_field({**VALID, field: 12345678})
    assert errors == {field: "Debe ser un texto"}


@pytest.mark.parametrize(
    "field", ["transaction_id", "document_type", "document_number", "full_name", "phone", "dealer"]
)
def test_required_fields(field: str) -> None:
    errors = errors_by_field({k: v for k, v in VALID.items() if k != field})
    assert errors == {field: "Campo obligatorio"}


def test_invalid_document_type_does_not_add_a_document_number_error() -> None:
    errors = errors_by_field({**VALID, "document_type": "LIBRETA", "document_number": "lo que sea"})
    assert list(errors) == ["document_type"]


def test_reports_all_invalid_fields_at_once() -> None:
    errors = errors_by_field({**VALID, "document_number": "1", "phone": "1", "full_name": ""})
    assert set(errors) == {"document_number", "phone", "full_name"}


# --- ConsentListQuery ---


def test_list_query_normalizes_document_and_dealer() -> None:
    query = ConsentListQuery.model_validate(
        {"document_type": "dni", "document_number": "1234-5678", "dealer": " DEALER-001 "}
    )
    assert query.document_type is DocumentType.DNI
    assert query.document_number == "12345678"
    assert query.dealer == "DEALER-001"


def test_list_query_dealer_is_optional() -> None:
    query = ConsentListQuery.model_validate({"document_type": "RUC", "document_number": "20123456789"})
    assert query.dealer is None


def test_list_query_validates_document() -> None:
    errors = errors_by_field({"document_type": "DNI", "document_number": "123"}, ConsentListQuery)
    assert list(errors) == ["document_number"]
