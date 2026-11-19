"""Genera las variables del servicio privado de KIZUNA (`consent-api-kizuna`) desde `deploy/cloudrun.env.yaml`.

INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4.4. `gcloud run deploy --env-vars-file` reemplaza todas las
variables y no se combina con `--update-env-vars`, así que el servicio privado necesita su propio archivo. Para no
mantener dos copias a mano, este script copia el del servicio público y solo cambia:

    API_SURFACE: "kizuna"
    KIZUNA_INBOUND_AUTH: "cloud_run_iam"

Uso (desde yamaha-consent-api/, después de cada cambio en deploy/cloudrun.env.yaml):

    uv run python -m scripts.kizuna_env            # → deploy/cloudrun-kizuna.env.yaml (ignorado por git)

Solo desarrollo y despliegue manual (como el resto de `scripts/`, no va en la imagen).
"""

import argparse
import re
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
DEFAULT_SOURCE = DEPLOY_DIR / "cloudrun.env.yaml"
DEFAULT_OUTPUT = DEPLOY_DIR / "cloudrun-kizuna.env.yaml"
OVERRIDES = {"API_SURFACE": "kizuna", "KIZUNA_INBOUND_AUTH": "cloud_run_iam"}
HEADER = (
    "# GENERADO por scripts/kizuna_env.py desde deploy/cloudrun.env.yaml: NO editar a mano.\n"
    "# Variables del servicio privado de KIZUNA (consent-api-kizuna, Cloud Run IAM). Igual al archivo del servicio\n"
    "# público salvo API_SURFACE y KIZUNA_INBOUND_AUTH (INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4.4).\n\n"
)


def kizuna_env_text(source: str) -> str:
    """El texto de `source` con los valores de OVERRIDES. Falla si alguna clave no aparece exactamente una vez."""
    text = source
    for key, value in OVERRIDES.items():
        pattern = re.compile(rf"^{key}:.*$", re.MULTILINE)
        found = len(pattern.findall(text))
        if found != 1:
            raise ValueError(
                f"{key} debe aparecer exactamente una vez en el archivo de origen (aparece {found})"
            )
        text = pattern.sub(f'{key}: "{value}"', text)
    return HEADER + text


def write_kizuna_env(source: Path = DEFAULT_SOURCE, output: Path = DEFAULT_OUTPUT) -> Path:
    text = kizuna_env_text(source.read_text(encoding="utf-8"))
    output.write_text(text, encoding="utf-8", newline="\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera el archivo de variables del servicio consent-api-kizuna."
    )
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE, help="Por defecto deploy/cloudrun.env.yaml"
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="Por defecto deploy/cloudrun-kizuna.env.yaml"
    )
    args = parser.parse_args()
    print(f"Variables de consent-api-kizuna escritas en {write_kizuna_env(args.source, args.output)}")


if __name__ == "__main__":
    main()
