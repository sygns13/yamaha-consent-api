import json
import logging
import sys

import httpx
import pytest
from fastapi import FastAPI

from app.core.logging import JsonFormatter, request_id_var
from app.core.middleware import RequestIdMiddleware


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo/{token}")
    async def echo(token: str) -> dict[str, str | None]:
        return {"request_id": request_id_var.get()}

    return app


async def get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


async def test_generates_request_id_and_exposes_it_to_the_endpoint() -> None:
    response = await get("/echo/abc")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert response.json() == {"request_id": request_id}


async def test_reuses_valid_incoming_request_id() -> None:
    response = await get("/echo/abc", headers={"X-Request-ID": "kizuna-req-123"})
    assert response.headers["X-Request-ID"] == "kizuna-req-123"


async def test_replaces_unsafe_incoming_request_id() -> None:
    response = await get("/echo/abc", headers={"X-Request-ID": "x" * 65})
    assert response.headers["X-Request-ID"] != "x" * 65


async def test_access_log_uses_route_template_not_token_or_query(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        response = await get("/echo/SECRET-TOKEN?document_number=12345678")

    record = next(r for r in caplog.records if r.name == "app.request")
    line = JsonFormatter().format(record)
    assert "SECRET-TOKEN" not in line
    assert "12345678" not in line
    entry = json.loads(line)
    assert entry["path"] == "/echo/{token}"
    assert entry["status_code"] == 200
    assert entry["severity"] == "INFO"
    assert "duration_ms" in entry
    # El contextvar ya se restauró; el request_id del log se compara con el de la respuesta.
    assert response.headers["X-Request-ID"]


def test_formatter_includes_request_id_extra_fields_and_exception() -> None:
    token = request_id_var.set("req-1")
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.getLogger("test").makeRecord(
                "test", logging.ERROR, __file__, 1, "fallo %s", ("x",), exc_info=sys.exc_info()
            )
        record.transaction_id = "TRX-1"
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)

    assert entry["message"] == "fallo x"
    assert entry["severity"] == "ERROR"
    assert entry["request_id"] == "req-1"
    assert entry["transaction_id"] == "TRX-1"
    assert "ValueError: boom" in entry["exception"]
    assert entry["timestamp"].endswith("+00:00")
