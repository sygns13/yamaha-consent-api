"""Exportación del OpenAPI para el frontend del BackOffice (BACKOFFICE_DEALERS.spec.md, criterio B5.12)."""

import json
from pathlib import Path

from scripts.export_openapi import write_openapi


def test_export_writes_the_same_document_as_openapi_json(tmp_path: Path) -> None:
    from app.main import app

    output = tmp_path / "docs" / "openapi.json"
    write_openapi(output)
    text = output.read_text(encoding="utf-8")
    assert text.endswith("}\n") and "\r\n" not in text
    document = json.loads(text)
    assert document == json.loads(json.dumps(app.openapi()))
    paths = document["paths"]
    assert "/api/v1/backoffice/dealers/{dealerId}" in paths
    assert "/api/v1/backoffice/users/{userId}/reset-password" in paths
    assert "/api/v1/backoffice/audit-logs" in paths
    assert "BackofficeSession" in document["components"]["securitySchemes"]
