"""Exporta el OpenAPI del backend (el mismo JSON que sirve `/openapi.json` con `DOCS_ENABLED=true`).

Solo desarrollo (como el resto de `scripts/`, no va en la imagen). Lo usa el frontend del BackOffice: su
`docs/openapi.json` es una instantánea de este archivo, de la que `npm run api:types` genera los tipos
(BACKOFFICE_DEALERS.spec.md, sección 8.2, criterio B5.12). Regenerarla cada vez que cambie la API:

    uv run python -m scripts.export_openapi ../yamaha-consent-backoffice/docs/openapi.json
"""

import argparse
import json
from pathlib import Path
from typing import Any


def openapi_document() -> dict[str, Any]:
    from app.main import app

    return app.openapi()


def write_openapi(path: Path) -> None:
    """UTF-8, sangría de 2 espacios y salto de línea final (diffs legibles en el repo del frontend)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(openapi_document(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Exporta el OpenAPI del backend a un archivo JSON.")
    parser.add_argument(
        "output", type=Path, help="Ruta del archivo (p. ej. ../yamaha-consent-backoffice/docs/openapi.json)"
    )
    args = parser.parse_args()
    write_openapi(args.output)
    print(f"OpenAPI exportado en {args.output}")


if __name__ == "__main__":
    main()
