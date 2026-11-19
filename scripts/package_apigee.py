"""Empaqueta los bundles de Apigee (`deploy/apigee/<proxy>/apiproxy/`) en zip listos para importar.

Solo desarrollo (como el resto de `scripts/`, no va en la imagen). No depende de `zip`, así que funciona igual en
Windows. INTEGRACION_KIZUNA_OAUTH2.spec.md, fase K2; los pasos de importación están en `deploy/apigee/README.md`.

    uv run python -m scripts.package_apigee                 # → deploy/apigee/dist/<proxy>.zip
    uv run python -m scripts.package_apigee --output /tmp/x
"""

import argparse
import zipfile
from pathlib import Path

APIGEE_DIR = Path(__file__).resolve().parents[1] / "deploy" / "apigee"
DEFAULT_OUTPUT = APIGEE_DIR / "dist"


def bundle_dirs(root: Path = APIGEE_DIR) -> list[Path]:
    """Carpetas de proxy: las que tienen un `apiproxy/` adentro."""
    return sorted(path for path in root.iterdir() if (path / "apiproxy").is_dir())


def package_bundle(bundle: Path, output_dir: Path) -> Path:
    """Zip con `apiproxy/...` en la raíz (lo que espera la importación de Apigee). Orden fijo: zips reproducibles."""
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{bundle.name}.zip"
    files = sorted(path for path in (bundle / "apiproxy").rglob("*") if path.is_file())
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(bundle).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return target


def package_all(output_dir: Path = DEFAULT_OUTPUT, root: Path = APIGEE_DIR) -> list[Path]:
    return [package_bundle(bundle, output_dir) for bundle in bundle_dirs(root)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Empaqueta los bundles de Apigee en zip.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Carpeta destino de los zip")
    args = parser.parse_args()
    for path in package_all(args.output):
        print(f"Bundle empaquetado: {path}")


if __name__ == "__main__":
    main()
