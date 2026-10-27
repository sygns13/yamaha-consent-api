"""Errores HTTP que no son de validación: en español y en JSON."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.http_errors import (
    BODY_PARSE_ERROR,
    INTERNAL_ERROR,
    http_exception_handler,
    translate_http_detail,
    unhandled_exception_handler,
)
from app.models import Consent

# --- Traducción ---


@pytest.mark.parametrize(
    ("status_code", "detail", "expected"),
    [
        (404, "Not Found", "Recurso no encontrado"),
        (405, "Method Not Allowed", "Método no permitido"),
        (400, "There was an error parsing the body", BODY_PARSE_ERROR),
        (500, "Internal Server Error", "Error interno del servidor"),
        (429, "Too Many Requests", "Demasiadas peticiones"),
        (418, "I'm a Teapot", "Error HTTP 418"),  # código sin traducción propia
        (599, "", "Error HTTP 599"),  # código no estándar
        # Nuestros textos en español pasan sin cambios.
        (401, "Falta la API Key", "Falta la API Key"),
        (404, "No existe la transacción TRX-1", "No existe la transacción TRX-1"),
    ],
)
def test_translate_http_detail(status_code: int, detail: str, expected: str) -> None:
    assert translate_http_detail(status_code, detail) == expected


def test_structured_details_pass_through() -> None:
    detail = {"link_status": "EXPIRED"}
    assert translate_http_detail(410, detail) is detail


# --- App real ---


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_unknown_route_returns_spanish_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/no-existe")
    assert response.status_code == 404
    assert response.json() == {"detail": "Recurso no encontrado"}


async def test_wrong_method_returns_spanish_405_and_keeps_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.delete("/health")
    assert response.status_code == 405
    assert response.json() == {"detail": "Método no permitido"}
    assert response.headers["allow"] == "GET"


async def test_unreadable_body_returns_spanish_400(client: httpx.AsyncClient) -> None:
    # JSON válido pero en Latin-1: "é" no es UTF-8 válido.
    body = '{"full_name": "José Ñuñez"}'.encode("latin-1")
    response = await client.post(
        "/api/v1/consents", content=body, headers={"Content-Type": "application/json", "X-API-Key": "x"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": BODY_PARSE_ERROR}


def test_main_app_registers_the_handlers() -> None:
    from app.main import app

    assert app.exception_handlers[StarletteHTTPException] is http_exception_handler
    assert app.exception_handlers[Exception] is unhandled_exception_handler
    assert RequestValidationError in app.exception_handlers


# --- App de prueba con errores provocados ---


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("fallo interno con el documento 12345678")

    @app.get("/plain/{code}")
    async def plain(code: int) -> None:
        raise HTTPException(status_code=code)

    @app.get("/own")
    async def own() -> None:
        raise HTTPException(status_code=409, detail="Mensaje propio", headers={"X-Extra": "1"})

    return app


async def call(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path)


async def test_unhandled_exception_returns_spanish_json_500_without_details() -> None:
    response = await call("/boom")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": INTERNAL_ERROR}
    assert "12345678" not in response.text


@pytest.mark.parametrize(
    ("code", "expected"),
    [(400, "Solicitud inválida"), (403, "Acceso denegado"), (503, "Servicio no disponible")],
)
async def test_http_exception_without_detail_is_translated(code: int, expected: str) -> None:
    response = await call(f"/plain/{code}")
    assert response.status_code == code
    assert response.json() == {"detail": expected}


async def test_own_detail_and_headers_are_kept() -> None:
    response = await call("/own")
    assert response.status_code == 409
    assert response.json() == {"detail": "Mensaje propio"}
    assert response.headers["x-extra"] == "1"


# --- Datos personales en errores de SQL ---


def test_app_engine_hides_sql_parameters() -> None:
    from app.db.session import engine

    assert engine.sync_engine.hide_parameters is True


async def test_integrity_error_message_has_no_personal_data(db_engine: AsyncEngine) -> None:
    row: dict[str, Any] = {
        "transaction_id": "TRX-DUP",
        "document_type": "DNI",
        "document_number": "12345678",
        "dealer": "D",
        "full_name": "JUAN SECRETO",
        "phone": "987654321",
    }
    async with db_engine.begin() as conn:
        await conn.execute(insert(Consent).values(**row))
    with pytest.raises(IntegrityError) as exc:
        async with db_engine.begin() as conn:
            await conn.execute(insert(Consent).values(**row))

    message = str(exc.value)
    assert "pk_consents" in message
    for secret in ("12345678", "JUAN SECRETO", "987654321"):
        assert secret not in message
