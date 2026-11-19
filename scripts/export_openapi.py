"""Exporta el OpenAPI del backend (el mismo JSON que sirve `/openapi.json` con `DOCS_ENABLED=true`).

Solo desarrollo (como el resto de `scripts/`, no va en la imagen). Lo usa el frontend del BackOffice: su
`docs/openapi.json` es una instantánea de este archivo, de la que `npm run api:types` genera los tipos
(BACKOFFICE_DEALERS.spec.md, sección 8.2, criterio B5.12). Regenerarla cada vez que cambie la API:

    uv run python -m scripts.export_openapi ../yamaha-consent-backoffice/docs/openapi.json

Con `--kizuna` exporta el contrato que se entrega a KIZUNA (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4.5):
solo los endpoints del tag `kizuna`, con la URL del servicio privado `consent-api-kizuna` en `servers` y un Bearer
(ID token de Google de la cuenta de servicio de KIZUNA, con la audiencia propia del servicio) en lugar de la API Key.
Sin argumentos usa marcadores; con los valores reales:

    uv run python -m scripts.export_openapi --kizuna --server https://<URL_CONSENT_API_KIZUNA> \
      --audience consent-api-kizuna.<PROJECT_ID> openapi-kizuna.json
"""

import argparse
import copy
import json
from pathlib import Path
from typing import Any

KIZUNA_TAG = "kizuna"
DEFAULT_SERVER = "https://<URL_CONSENT_API_KIZUNA>"
DEFAULT_AUDIENCE = "consent-api-kizuna.<PROJECT_ID>"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
BEARER_SCHEME = "GoogleIdToken"
CLOUD_RUN_RESPONSES = {
    "401": (
        "ID token ausente, inválido, vencido o con otra audiencia: de Cloud Run (página de error de Google, no JSON) o "
        'de la API (`{"detail": "..."}`)'
    ),
    "403": (
        "La identidad no tiene permiso para invocar el servicio (Cloud Run, no JSON) o no es la cuenta de servicio de "
        'KIZUNA (API, `{"detail": "..."}`)'
    ),
    "503": 'La API no pudo obtener las claves públicas de Google para validar el token; reintentar (`{"detail": "..."}`)',
}


def openapi_document() -> dict[str, Any]:
    from app.main import app

    return app.openapi()


def kizuna_openapi_document(server: str = DEFAULT_SERVER, audience: str = DEFAULT_AUDIENCE) -> dict[str, Any]:
    """Solo las operaciones del tag `kizuna`, con el Bearer de Cloud Run IAM y la URL del servicio privado."""
    server = server.rstrip("/")
    document = copy.deepcopy(openapi_document())
    paths: dict[str, Any] = {}
    for path, operations in document["paths"].items():
        kept = {method: op for method, op in operations.items() if KIZUNA_TAG in op.get("tags", [])}
        for operation in kept.values():
            operation["security"] = [{BEARER_SCHEME: []}]
            for code, description in CLOUD_RUN_RESPONSES.items():
                operation["responses"].setdefault(code, {"description": description})
        if kept:
            paths[path] = kept

    document["paths"] = paths
    document["servers"] = [{"url": server, "description": "Cloud Run consent-api-kizuna (Producción)"}]
    document["info"]["title"] = "Yamaha Consent API — KIZUNA"
    document["info"]["description"] = (
        "Endpoints para KIZUNA en el servicio privado de Cloud Run. Autenticación: `Authorization: Bearer <ID token>`, "
        "un ID token de Google de la cuenta de servicio entregada a KIZUNA, con audiencia (`target_audience`) = "
        f"`{audience}` (no la URL). Se obtiene firmando un JWT con la clave de la cuenta de servicio y canjeándolo en "
        f"`POST {GOOGLE_TOKEN_URL}` (`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`); las librerías de "
        "Google lo hacen solas. Vigencia: 3600 s. Cloud Run valida el token antes de que la petición llegue a la API, "
        "y la API vuelve a verificarlo (firma, audiencia y cuenta de servicio). Usar el header `Authorization`."
    )
    document["components"]["securitySchemes"] = {
        BEARER_SCHEME: {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "ID token de Google de la cuenta de servicio de KIZUNA (`aud` = la audiencia propia del servicio). "
                "Lo validan Cloud Run IAM y la API."
            ),
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
        "--kizuna", action="store_true", help="Solo los endpoints de KIZUNA, con el Bearer de Cloud Run IAM"
    )
    parser.add_argument(
        "--server", default=DEFAULT_SERVER, help="URL del servicio consent-api-kizuna (con --kizuna)"
    )
    parser.add_argument(
        "--audience",
        default=DEFAULT_AUDIENCE,
        help="Audiencia del ID token = KIZUNA_ID_TOKEN_AUDIENCES (con --kizuna)",
    )
    args = parser.parse_args()
    if args.kizuna:
        write_json(args.output, kizuna_openapi_document(args.server, args.audience))
    else:
        write_openapi(args.output)
    print(f"OpenAPI exportado en {args.output}")


if __name__ == "__main__":
    main()
