import logging
from datetime import date, datetime
from typing import Annotated, Any, Literal, get_args
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core.core_schema import ErrorType

from app.core.validation_errors import (
    _MESSAGES,
    DEFAULT_MESSAGE,
    request_validation_exception_handler,
    translate_errors,
    translate_message,
)
from app.schemas import ConsentCreateRequest, ConsentListQuery


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = "x"
    short: str = Field(default="xx", min_length=2)
    long: str = Field(default="x", max_length=1)
    pattern: str = Field(default="a", pattern=r"^a$")
    choice: Literal["A", "B", "C"] = "A"
    flag: bool = True
    integer: int = 1
    positive: int = Field(default=1, gt=0)
    at_least_one: int = Field(default=1, ge=1)
    below_ten: float = Field(default=1, lt=10)
    at_most_ten: float = Field(default=1, le=10)
    number: float = 1.0
    items: list[int] = Field(default_factory=lambda: [1], min_length=1, max_length=2)
    mapping: dict[str, Any] = Field(default_factory=dict)
    moment: datetime = datetime(2026, 1, 1)
    day: date = date(2026, 1, 1)
    identifier: UUID = UUID(int=0)


def message_for(**data: Any) -> str:
    with pytest.raises(ValidationError) as exc:
        Sample.model_validate(data)
    (error,) = exc.value.errors()
    return translate_message(error)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"text": 1}, "Debe ser un texto"),
        ({"short": "x"}, "Debe tener al menos 2 caracteres"),
        ({"long": "xx"}, "Debe tener como máximo 1 carácter"),
        ({"pattern": "b"}, "No tiene el formato esperado"),
        ({"choice": "Z"}, "Debe ser uno de estos valores: 'A', 'B' o 'C'"),
        ({"flag": "quizás"}, "Debe ser verdadero o falso (true/false)"),
        ({"flag": []}, "Debe ser verdadero o falso (true/false)"),
        ({"integer": "uno"}, "Debe ser un número entero"),
        ({"integer": 1.5}, "Debe ser un número entero, sin decimales"),
        ({"integer": []}, "Debe ser un número entero"),
        ({"positive": 0}, "Debe ser mayor que 0"),
        ({"at_least_one": 0}, "Debe ser mayor o igual que 1"),
        ({"below_ten": 10}, "Debe ser menor que 10"),
        ({"at_most_ten": 11}, "Debe ser menor o igual que 10"),
        ({"number": "x"}, "Debe ser un número"),
        ({"items": []}, "Debe tener al menos 1 elemento"),
        ({"items": [1, 2, 3]}, "Debe tener como máximo 2 elementos"),
        ({"items": "x"}, "Debe ser una lista"),
        ({"mapping": []}, "Debe ser un objeto JSON"),
        ({"moment": "ayer"}, "Debe ser una fecha y hora válida (ISO 8601)"),
        ({"day": "ayer"}, "Debe ser una fecha válida (AAAA-MM-DD)"),
        ({"identifier": "x"}, "Debe ser un UUID válido"),
        ({"otro": 1}, "Campo no permitido"),
    ],
)
def test_translates_pydantic_messages(data: dict[str, Any], expected: str) -> None:
    assert message_for(**data) == expected


def test_missing_field() -> None:
    class Required(BaseModel):
        name: str

    with pytest.raises(ValidationError) as exc:
        Required.model_validate({})
    assert translate_errors(exc.value.errors()) == [
        {"type": "missing", "loc": ["name"], "msg": "Campo obligatorio"}
    ]


def test_own_value_error_drops_english_prefix() -> None:
    with pytest.raises(ValidationError) as exc:
        ConsentListQuery.model_validate({"document_type": "DNI", "document_number": "1"})
    (error,) = exc.value.errors()
    assert error["msg"].startswith("Value error, ")
    assert translate_message(error) == "El DNI debe tener exactamente 8 dígitos"


def test_unknown_type_falls_back_to_spanish() -> None:
    assert translate_message({"type": "tipo_que_no_existe", "loc": ("x",)}) == DEFAULT_MESSAGE


def test_missing_context_falls_back_to_spanish() -> None:
    assert translate_message({"type": "string_too_long", "loc": ("x",)}) == DEFAULT_MESSAGE


def test_translation_table_only_uses_real_pydantic_types() -> None:
    # Evita claves mal escritas que nunca se usarían.
    assert set(_MESSAGES) - set(get_args(ErrorType)) == set()


# --- Respuesta HTTP 422 ---


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

    @app.post("/consents")
    async def create(body: ConsentCreateRequest) -> dict[str, str]:
        return {"ok": "sí"}

    @app.get("/consents")
    async def search(query: Annotated[ConsentListQuery, Query()]) -> dict[str, str]:
        return {"ok": "sí"}

    return app


async def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


BODY = {
    "transaction_id": "TRX-1",
    "document_type": "DNI",
    "document_number": "1234567",
    "full_name": "JUAN PÉREZ SECRETO",
    "phone": "98765",
    "dealer": "DEALER-001",
}


async def test_422_response_is_in_spanish_and_has_no_input(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.validation_errors"):
        response = await request("POST", "/consents", json={**BODY, "full_name": ""})

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "document_number"],
                "msg": "El DNI debe tener exactamente 8 dígitos",
            },
            {"type": "value_error", "loc": ["body", "full_name"], "msg": "El nombre no puede estar vacío"},
            {
                "type": "value_error",
                "loc": ["body", "phone"],
                "msg": "El teléfono debe tener exactamente 9 dígitos (formato XXXXXXXXX)",
            },
        ]
    }
    # Ni la respuesta ni el log devuelven lo que se envió.
    for sent in ("1234567", "98765"):
        assert sent not in response.text
        assert sent not in caplog.text
    record = next(r for r in caplog.records if r.name == "app.core.validation_errors")
    assert record.errors == [
        {"loc": ["body", "document_number"], "type": "value_error"},
        {"loc": ["body", "full_name"], "type": "value_error"},
        {"loc": ["body", "phone"], "type": "value_error"},
    ]


async def test_422_when_body_is_missing() -> None:
    response = await request("POST", "/consents")
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "missing", "loc": ["body"], "msg": "El cuerpo de la petición es obligatorio"}
    ]


async def test_422_when_body_is_not_an_object() -> None:
    response = await request("POST", "/consents", json=[1, 2])
    assert response.json()["detail"] == [
        {
            "type": "model_attributes_type",
            "loc": ["body"],
            "msg": "El cuerpo de la petición debe ser un objeto JSON",
        }
    ]


async def test_422_when_json_is_invalid() -> None:
    response = await request(
        "POST", "/consents", content=b'{"transaction_id": ', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["type"] == "json_invalid"
    assert error["msg"] == "El cuerpo de la petición no es un JSON válido"


async def test_422_on_query_params() -> None:
    response = await request("GET", "/consents", params={"document_type": "dni"})
    assert response.json()["detail"] == [
        {"type": "missing", "loc": ["query", "document_number"], "msg": "Campo obligatorio"}
    ]


async def test_valid_request_is_not_affected() -> None:
    response = await request(
        "POST", "/consents", json={**BODY, "document_number": "12345678", "phone": "987654321"}
    )
    assert response.status_code == 200


def test_main_app_registers_the_handler() -> None:
    from app.main import app

    assert app.exception_handlers[RequestValidationError] is request_validation_exception_handler
