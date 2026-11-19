"""OpenAPI que se entrega a KIZUNA: solo sus endpoints, con el Bearer de Cloud Run IAM.

INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4.5 (criterio I1.7).
"""

import json
from pathlib import Path

import pytest

from scripts.export_openapi import kizuna_openapi_document, main, openapi_document

SERVER = "https://consent-api-kizuna-ejemplo.a.run.app"
AUDIENCE = "consent-api-kizuna.proyecto-de-prueba"


def test_only_the_kizuna_operations_are_exported() -> None:
    document = kizuna_openapi_document(SERVER)
    operations = {(method, path) for path, ops in document["paths"].items() for method in ops}
    assert operations == {
        ("post", "/api/v1/consents"),
        ("get", "/api/v1/consents"),
        ("get", "/api/v1/consents/{IdentifierId}"),
    }
    assert all("kizuna" in op["tags"] for ops in document["paths"].values() for op in ops.values())


def test_google_id_token_bearer_replaces_the_api_key() -> None:
    document = kizuna_openapi_document(f"{SERVER}/", AUDIENCE)
    assert document["servers"] == [
        {"url": SERVER, "description": "Cloud Run consent-api-kizuna (Producción)"}
    ]
    schemes = document["components"]["securitySchemes"]
    assert schemes == {
        "GoogleIdToken": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": schemes["GoogleIdToken"]["description"],
        }
    }
    for ops in document["paths"].values():
        for operation in ops.values():
            assert operation["security"] == [{"GoogleIdToken": []}]
            assert {"401", "403", "503"} <= set(operation["responses"])
    description = document["info"]["description"]
    assert f"`{AUDIENCE}` (no la URL)" in description
    assert "`Authorization`" in description
    assert "https://oauth2.googleapis.com/token" in description
    dumped = json.dumps(document)
    for forbidden in ("ApiKey", "X-API-Key", "Apigee", "apigee", "clientCredentials"):
        assert forbidden not in dumped


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
    monkeypatch.setattr(
        "sys.argv", ["export_openapi", "--kizuna", "--server", SERVER, "--audience", AUDIENCE, str(output)]
    )
    main()
    text = output.read_text(encoding="utf-8")
    assert text.endswith("}\n") and "\r\n" not in text
    assert json.loads(text)["servers"][0]["url"] == SERVER
    assert AUDIENCE in json.loads(text)["info"]["description"]
    assert "OpenAPI exportado" in capsys.readouterr().out


def test_cli_rejects_the_removed_token_url_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "sys.argv", ["export_openapi", "--kizuna", "--token-url", "x", str(tmp_path / "o.json")]
    )
    with pytest.raises(SystemExit):
        main()
