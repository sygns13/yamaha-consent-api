"""Exporta el OpenAPI del backend (el mismo JSON que sirve `/openapi.json` con `DOCS_ENABLED=true`).

Solo desarrollo (como el resto de `scripts/`, no va en la imagen). Lo usa el frontend del BackOffice: su
`docs/openapi.json` es una instantánea de este archivo, de la que `npm run api:types` genera los tipos
(BACKOFFICE_DEALERS.spec.md, sección 8.2, criterio B5.12). Regenerarla cada vez que cambie la API:

    uv run python -m scripts.export_openapi ../yamaha-consent-backoffice/docs/openapi.json

Con `--kizuna` exporta el contrato que se entrega a KIZUNA (INTEGRACION_KIZUNA_OAUTH2.spec.md, K2 tarea 6): solo los
endpoints del tag `kizuna`, con el host de Apigee en `servers` y OAuth 2.0 client credentials en lugar de la API Key
(que KIZUNA ya no usa: la pone Apigee). Sin argumentos usa marcadores; con el dominio real:

    uv run python -m scripts.export_openapi --kizuna --server https://<API_HOST> openapi-kizuna.json
"""

import argparse
import copy
import json
from pathlib import Path
from typing import Any

KIZUNA_TAG = "kizuna"
KIZUNA_SCOPE = "kizuna.consents"
DEFAULT_SERVER = "https://<API_HOST>"
TOKEN_PATH = "/oauth2/token"
OAUTH_SCHEME = "OAuth2"
APIGEE_RESPONSES = {
    "401": 'Falta el token de acceso, o es inválido o vencido (Apigee): `{"detail": "..."}`',
    "403": "El token no tiene el scope `kizuna.consents` (Apigee)",
}


def openapi_document() -> dict[str, Any]:
    from app.main import app

    return app.openapi()


def kizuna_openapi_document(server: str = DEFAULT_SERVER, token_url: str | None = None) -> dict[str, Any]:
    """Solo las operaciones del tag `kizuna`, con OAuth 2.0 client credentials y el host de Apigee."""
    server = server.rstrip("/")
    document = copy.deepcopy(openapi_document())
    paths: dict[str, Any] = {}
    for path, operations in document["paths"].items():
        kept = {method: op for method, op in operations.items() if KIZUNA_TAG in op.get("tags", [])}
        for operation in kept.values():
            operation["security"] = [{OAUTH_SCHEME: [KIZUNA_SCOPE]}]
            for code, description in APIGEE_RESPONSES.items():
                operation["responses"].setdefault(code, {"description": description})
        if kept:
            paths[path] = kept

    document["paths"] = paths
    document["servers"] = [{"url": server, "description": "Apigee (Producción)"}]
    document["info"]["title"] = "Yamaha Consent API — KIZUNA"
    document["info"]["description"] = (
        f"Endpoints para KIZUNA vía Apigee. Token: `POST {token_url or server + TOKEN_PATH}` con "
        "`grant_type=client_credentials`, `client_id`, `client_secret` y `scope=kizuna.consents` (en el cuerpo "
        "form-urlencoded o con Basic); se envía como `Authorization: Bearer <token>`. Vigencia: 3600 s."
    )
    document["components"]["securitySchemes"] = {
        OAUTH_SCHEME: {
            "type": "oauth2",
            "description": "OAuth 2.0 client credentials emitido por Apigee",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": token_url or f"{server}{TOKEN_PATH}",
                    "scopes": {KIZUNA_SCOPE: "Registrar y consultar consentimientos"},
                }
            },
        }
    }
    _keep_referenced_schemas(document)
    return document


def _keep_referenced_schemas(document: dict[str, Any]) -> None:
    """Quita los esquemas que las operaciones de KIZUNA no usan (los del BackOffice, la landing...)."""
    schemas = document["components"].get("schemas", {})
    prefix = "#/components/schemas/"
    pending = [document["paths"]]
    used: set[str] = set()
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(prefix) and ref[len(prefix) :] not in used:
                name = ref[len(prefix) :]
                used.add(name)
                pending.append(schemas[name])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    document["components"]["schemas"] = {name: schemas[name] for name in sorted(used)}


def write_json(path: Path, document: dict[str, Any]) -> None:
    """UTF-8, sangría de 2 espacios y salto de línea final (diffs legibles en el repo del frontend)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def write_openapi(path: Path) -> None:
    write_json(path, openapi_document())


def main() -> None:
    parser = argparse.ArgumentParser(description="Exporta el OpenAPI del backend a un archivo JSON.")
    parser.add_argument(
        "output", type=Path, help="Ruta del archivo (p. ej. ../yamaha-consent-backoffice/docs/openapi.json)"
    )
    parser.add_argument(
        "--kizuna", action="store_true", help="Solo los endpoints de KIZUNA, con OAuth 2.0 de Apigee"
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="URL base de Apigee (con --kizuna)")
    parser.add_argument(
        "--token-url", default=None, help="Token URL (con --kizuna; por defecto <server>/oauth2/token)"
    )
    args = parser.parse_args()
    if args.kizuna:
        write_json(args.output, kizuna_openapi_document(args.server, args.token_url))
    else:
        write_openapi(args.output)
    print(f"OpenAPI exportado en {args.output}")


if __name__ == "__main__":
    main()
