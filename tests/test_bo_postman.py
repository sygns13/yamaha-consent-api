"""Carpeta "BackOffice" de `postman/collection.json` (BACKOFFICE_DEALERS.spec.md, fase B5): cubre todos los endpoints
del BackOffice del OpenAPI y toda mutación lleva la cabecera CSRF."""

import json
import re
from pathlib import Path
from typing import Any

from fastapi.openapi.utils import get_openapi

COLLECTION = Path(__file__).resolve().parents[1] / "postman" / "collection.json"
PREFIX = "/api/v1/backoffice"
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}


def backoffice_folder() -> dict[str, Any]:
    collection = json.loads(COLLECTION.read_text(encoding="utf-8"))
    (folder,) = [item for item in collection["item"] if item["name"] == "BackOffice"]
    return folder


def requests(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for item in items:
        if "item" in item:
            found.extend(requests(item["item"]))
        else:
            found.append(item["request"])
    return found


def openapi_operations() -> list[tuple[str, str]]:
    from app.main import app

    schema = get_openapi(title="test", version="0", routes=app.routes)
    return [
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        if path.startswith(PREFIX)
        for method in operations
    ]


def test_folder_has_no_api_key_and_variables_exist() -> None:
    folder = backoffice_folder()
    assert folder["auth"] == {"type": "noauth"}
    collection = json.loads(COLLECTION.read_text(encoding="utf-8"))
    variables = {variable["key"] for variable in collection["variable"]}
    assert {"backoffice_base_url", "bo_username", "bo_password", "user_id", "dealer_id"} <= variables
    # Nunca una contraseña real versionada.
    assert all(
        variable["value"] == ""
        for variable in collection["variable"]
        if variable["key"] in {"bo_password", "bo_new_password", "reset_token"}
    )


def test_every_backoffice_endpoint_has_a_request() -> None:
    collected = [
        (request["method"], "/" + "/".join(request["url"]["path"]))
        for request in requests(backoffice_folder()["item"])
    ]
    for method, template in openapi_operations():
        pattern = re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$")
        assert any(m == method and pattern.match(path) for m, path in collected), (method, template)


def test_mutations_send_the_csrf_header_and_use_the_backoffice_url() -> None:
    for request in requests(backoffice_folder()["item"]):
        assert request["url"]["raw"].startswith("{{backoffice_base_url}}/api/v1/backoffice/")
        headers = {header["key"]: header["value"] for header in request.get("header", [])}
        if request["method"] in MUTATIONS:
            assert headers.get("X-Requested-With") == "XMLHttpRequest", request["url"]["raw"]
        if "body" in request:
            json.loads(re.sub(r"\{\{\w+\}\}", "0", request["body"]["raw"]))
