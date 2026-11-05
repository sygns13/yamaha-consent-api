from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.validation_errors import translate_errors
from app.schemas import ConsentCreateRequest, ConsentListQuery, ConsentRequestData

# Ejemplo del contrato de KIZUNA (contenido de `_request`).
VALID = {
    "IdentifierId": "100002001",
    "customerId": "76299887",
    "customerName": "Nombre",
    "customerPhone": "+51999111222",
    "customerEmail": "nombre@email.com",
    "dealerName": "ABC Motors",
    "dealerCode": "CL0001",
    "requestDateTime": "2026-11-02T10:35:00+05:00",
}


def errors_by_field(data: dict[str, Any], model: type = ConsentRequestData) -> dict[str, str]:
    """Errores {campo: mensaje en español}, tal como los recibe KIZUNA en el 422."""
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    return {".".join(str(p) for p in err["loc"]): err["msg"] for err in translate_errors(exc.value.errors())}


def test_kizuna_example_is_normalized() -> None:
    request = ConsentCreateRequest.model_validate({"_request": VALID}).request
    assert request.transaction_id == "100002001"
    assert request.customer_id == "76299887"
    assert request.full_name == "NOMBRE"
    assert request.phone == "999111222"
    assert request.email == "nombre@email.com"
    assert request.dealer_name == "ABC Motors"
    assert request.dealer_code == "CL0001"
    assert request.requested_at == datetime(2026, 11, 2, 10, 35, tzinfo=timezone(timedelta(hours=5)))
    assert request.additional_info is None


def test_request_wrapper_is_required() -> None:
    errors = errors_by_field(VALID, ConsentCreateRequest)
    assert errors == {"_request": "Campo obligatorio"}


def test_normalizes_every_field() -> None:
    request = ConsentRequestData.model_validate(
        {
            **VALID,
            "customerId": " ab-1234 5678 ",
            "customerName": "  josé   ñuñez  ",
            "customerPhone": "+51 987-654-321",
            "customerEmail": "  Nombre@Email.COM ",
            "dealerName": "  ABC    Motors  ",
            "dealerCode": "  CL0001  ",
        }
    )
    assert request.customer_id == "AB12345678"
    assert request.full_name == "JOSÉ ÑUÑEZ"
    assert request.phone == "987654321"
    assert request.email == "nombre@email.com"
    assert request.dealer_name == "ABC Motors"
    assert request.dealer_code == "CL0001"


@pytest.mark.parametrize("field", ["customerEmail", "dealerName", "requestDateTime", "additionalInfo"])
def test_optional_fields(field: str) -> None:
    request = ConsentRequestData.model_validate({k: v for k, v in VALID.items() if k != field})
    assert request.model_dump(by_alias=True)[field] is None


@pytest.mark.parametrize("email", ["sin-arroba", "a@b", "a b@c.com", "", 123, {"x": 1}, "x" * 250 + "@a.com"])
def test_invalid_email_becomes_null_without_error(email: Any) -> None:
    assert ConsentRequestData.model_validate({**VALID, "customerEmail": email}).email is None


@pytest.mark.parametrize("name", ["", "   ", 123])
def test_empty_or_non_text_dealer_name_becomes_null(name: Any) -> None:
    assert ConsentRequestData.model_validate({**VALID, "dealerName": name}).dealer_name is None


def test_request_datetime_without_offset_is_lima_time() -> None:
    request = ConsentRequestData.model_validate({**VALID, "requestDateTime": "2026-11-02T10:35:00"})
    assert request.requested_at == datetime(2026, 11, 2, 15, 35, tzinfo=UTC)


def test_additional_info_keeps_nested_json() -> None:
    info = {"canal": "tienda", "items": [1, 2], "meta": {"a": None}}
    assert ConsentRequestData.model_validate({**VALID, "additionalInfo": info}).additional_info == info


def test_transaction_id_is_kept_as_sent() -> None:
    # Es la PK que KIZUNA usa luego para consultar: no se modifica.
    request = ConsentRequestData.model_validate({**VALID, "IdentifierId": "trx-00 1"})
    assert request.transaction_id == "trx-00 1"


def test_unknown_fields_are_ignored() -> None:
    request = ConsentRequestData.model_validate({**VALID, "document_type": "DNI", "otro": 1})
    assert request.customer_id == "76299887"


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"customerId": "A" * 21}, "customerId", "máximo 20"),
        ({"customerId": "X.1"}, "customerId", "alfanumérico"),
        ({"customerId": "AÉ123"}, "customerId", "alfanumérico"),
        ({"customerId": "  "}, "customerId", "alfanumérico"),
        ({"customerName": "   "}, "customerName", "vacío"),
        ({"customerName": "A" * 251}, "customerName", "250"),
        ({"customerPhone": "987 654 32"}, "customerPhone", "9 dígitos"),
        ({"customerPhone": "+52 987 654 321"}, "customerPhone", "9 dígitos"),
        ({"dealerCode": "   "}, "dealerCode", "vacío"),
        ({"dealerCode": "D" * 101}, "dealerCode", "100"),
        ({"IdentifierId": ""}, "IdentifierId", "No puede estar vacío"),
        ({"IdentifierId": "   "}, "IdentifierId", "El IdentifierId no puede estar vacío"),
        ({"IdentifierId": "T" * 101}, "IdentifierId", "Debe tener como máximo 100 caracteres"),
        ({"requestDateTime": "02/11/2026"}, "requestDateTime", "ISO 8601"),
        ({"additionalInfo": [1, 2]}, "additionalInfo", "Debe ser un objeto JSON"),
    ],
)
def test_invalid_field_reports_that_field(overrides: dict[str, Any], field: str, message: str) -> None:
    errors = errors_by_field({**VALID, **overrides})
    assert list(errors) == [field]
    assert message in errors[field]
    assert not errors[field].startswith("Value error")


def test_customer_id_with_enie_is_accepted() -> None:
    assert ConsentRequestData.model_validate({**VALID, "customerId": "peña-1234"}).customer_id == "PEÑA1234"


@pytest.mark.parametrize("field", ["customerId", "customerPhone"])
def test_numbers_must_be_strings(field: str) -> None:
    # Un número JSON perdería los ceros a la izquierda (01234567): se exige texto.
    errors = errors_by_field({**VALID, field: 12345678})
    assert errors == {field: "Debe ser un texto"}


@pytest.mark.parametrize(
    "field", ["IdentifierId", "customerId", "customerName", "customerPhone", "dealerCode"]
)
def test_required_fields(field: str) -> None:
    errors = errors_by_field({k: v for k, v in VALID.items() if k != field})
    assert errors == {field: "Campo obligatorio"}


def test_reports_all_invalid_fields_at_once() -> None:
    errors = errors_by_field({**VALID, "customerId": "1.", "customerPhone": "1", "customerName": ""})
    assert set(errors) == {"customerId", "customerPhone", "customerName"}


# --- ConsentListQuery ---


def test_list_query_normalizes_customer_and_dealer() -> None:
    query = ConsentListQuery.model_validate({"customerId": "7629-9887", "dealerCode": " CL0001 "})
    assert query.customer_id == "76299887"
    assert query.dealer_code == "CL0001"


def test_list_query_dealer_is_optional() -> None:
    assert ConsentListQuery.model_validate({"customerId": "20123456789"}).dealer_code is None


def test_list_query_validates_customer_id() -> None:
    errors = errors_by_field({"customerId": "12.3"}, ConsentListQuery)
    assert list(errors) == ["customerId"]
