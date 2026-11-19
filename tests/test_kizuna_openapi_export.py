"""K2, tarea 6: OpenAPI que se entrega a KIZUNA (solo sus endpoints, con OAuth 2.0 de Apigee)."""

import json
from pathlib import Path

import pytest

from scripts.export_openapi import kizuna_openapi_document, main, openapi_document

SERVER = "https://api.ejemplo.test"


def test_only_the_kizuna_operations_are_exported() -> None:
    document = kizuna_openapi_document(SERVER)
    operations = {(method, path) for path, ops in document["paths"].items() for method in ops}
    assert operations == {
        ("post", "/api/v1/consents"),
        ("get", "/api/v1/consents"),
        ("get", "/api/v1/consents/{IdentifierId}"),
    }
    assert all("kizuna" in op["tags"] for ops in document["paths"].values() for op in ops.values())


def test_oauth2_client_credentials_replaces_the_api_key() -> None:
    document = kizuna_openapi_document(f"{SERVER}/")
    assert document["servers"] == [{"url": SERVER, "description": "Apigee (Producción)"}]
    schemes = document["components"]["securitySchemes"]
    assert list(schemes) == ["OAuth2"]
    flow = schemes["OAuth2"]["flows"]["clientCredentials"]
    assert flow["tokenUrl"] == f"{SERVER}/oauth2/token"
    assert list(flow["scopes"]) == ["kizuna.consents"]
    for ops in document["paths"].values():
        for operation in ops.values():
            assert operation["security"] == [{"OAuth2": ["kizuna.consents"]}]
            assert {"401", "403"} <= set(operation["responses"])
    assert "ApiKey" not in json.dumps(document)
    assert "X-API-Key" not in json.dumps(document)


def test_token_url_can_be_given_explicitly() -> None:
    document = kizuna_openapi_document(SERVER, "https://token.ejemplo.test/oauth2/token")
    assert document["components"]["securitySchemes"]["OAuth2"]["flows"]["clientCredentials"]["tokenUrl"] == (
        "https://token.ejemplo.test/oauth2/token"
    )


def test_only_referenced_schemas_are_kept() -> None:
    document = kizuna_openapi_document(SERVER)
    schemas = set(document["components"]["schemas"])
    dumped = json.dumps(document["paths"]) + json.dumps(document["components"]["schemas"])
    assert schemas
    assert all(f"#/components/schemas/{name}" in dumped for name in schemas)
    assert not any(name.startswith(("Backoffice", "Dealer", "User")) for name in schemas)


def test_the_full_document_is_not_modified() -> None:
    before = json.dumps(openapi_document(), sort_keys=True)
    kizuna_openapi_document(SERVER)
    assert json.dumps(openapi_document(), sort_keys=True) == before
    assert "ApiKey" in openapi_document()["components"]["securitySchemes"]


def test_cli_writes_the_kizuna_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "openapi-kizuna.json"
    monkeypatch.setattr("sys.argv", ["export_openapi", "--kizuna", "--server", SERVER, str(output)])
    main()
    text = output.read_text(encoding="utf-8")
    assert text.endswith("}\n") and "\r\n" not in text
    assert json.loads(text)["servers"][0]["url"] == SERVER
    assert "OpenAPI exportado" in capsys.readouterr().out
