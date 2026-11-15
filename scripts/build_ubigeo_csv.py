"""Solo desarrollo (fase B1): construye el catálogo Ubigeo INEI desde la fuente fijada en la sección 4.8 del spec del
BackOffice (`requerimientos/BACKOFFICE_DEALERS.spec.md`).

Descarga `ubigeo_distrito.csv` del repositorio público `jmcastagnetto/ubigeo-peru-aumentado` (licencia MIT) fijado a un
commit, verifica su SHA-256, lo transforma y escribe:

- `alembic/data/ubigeo_inei.csv`: `ubigeo,departamento,provincia,distrito`, UTF-8 sin BOM, fin de línea LF, ordenado
  por `ubigeo`. Lo carga la migración `ubigeo_inei_seed`.
- `alembic/data/UBIGEO_FUENTE.md`: fuente, commit, hashes, transformación, conteos, registro excluido y licencia.

Nunca completa ni corrige datos: si la descarga falla, el hash no coincide o algún conteo o validación no cuadra, se
detiene (código de salida 1) sin escribir nada.

Uso (desde yamaha-consent-api/; necesita acceso a raw.githubusercontent.com):
    uv run python -m scripts.build_ubigeo_csv

Se ejecuta una sola vez y su resultado se versiona. Como el resto de `scripts/`, no va en la imagen.
"""

import csv
import hashlib
import io
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

SOURCE_REPOSITORY = "jmcastagnetto/ubigeo-peru-aumentado"
SOURCE_COMMIT = "5e98222bb010243592ae02bfafb0a3658d6c4bd8"
SOURCE_COMMIT_DATE = "24/08/2021"
SOURCE_FILE = "ubigeo_distrito.csv"
SOURCE_URL = f"https://raw.githubusercontent.com/{SOURCE_REPOSITORY}/{SOURCE_COMMIT}/{SOURCE_FILE}"
LICENSE_URL = f"https://raw.githubusercontent.com/{SOURCE_REPOSITORY}/{SOURCE_COMMIT}/LICENSE"
SOURCE_SHA256 = "6a2cde8fb82e336285da4d684241450e8a36630e42799652c8d72b596ec252e4"

CODE_COLUMN = "inei"
NAME_COLUMNS = ("departamento", "provincia", "distrito")
# Largo de las columnas name de ubigeo_departments, ubigeo_provinces y ubigeo_districts (sección 4.3).
MAX_NAME_LENGTHS = {"departamento": 60, "provincia": 80, "distrito": 100}
OUTPUT_HEADER = ("ubigeo", *NAME_COLUMNS)
DISTRICT_CODE = re.compile(r"[0-9]{6}")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "alembic" / "data"
OUTPUT_CSV = DATA_DIR / "ubigeo_inei.csv"
OUTPUT_DOC = DATA_DIR / "UBIGEO_FUENTE.md"


class UbigeoSourceError(Exception):
    """La fuente no cumple lo esperado: hay que detenerse y avisar al responsable."""


@dataclass(frozen=True)
class ExpectedCounts:
    read: int
    excluded: int
    districts: int
    provinces: int
    departments: int


# Conteos del commit fijado (sección 4.8, paso 7). Cualquier otro resultado detiene el script.
EXPECTED_COUNTS = ExpectedCounts(read=1893, excluded=1, districts=1892, provinces=196, departments=25)


@dataclass
class UbigeoDataset:
    read: int = 0
    # (ubigeo, departamento, provincia, distrito), ordenado por ubigeo.
    districts: list[tuple[str, str, str, str]] = field(default_factory=list)
    provinces: dict[str, str] = field(default_factory=dict)
    departments: dict[str, str] = field(default_factory=dict)
    # Registros excluidos, con los campos que los identifican.
    excluded: list[dict[str, str]] = field(default_factory=list)
    # Códigos de los registros con un salto de línea dentro de algún campo (entre comillas).
    multiline: list[str] = field(default_factory=list)
    line_ending: str = ""

    def counts(self) -> ExpectedCounts:
        return ExpectedCounts(
            read=self.read,
            excluded=len(self.excluded),
            districts=len(self.districts),
            provinces=len(self.provinces),
            departments=len(self.departments),
        )


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def download(url: str, *, timeout: float = 60) -> bytes:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise UbigeoSourceError(f"No se pudo descargar {url}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise UbigeoSourceError(f"No se pudo descargar {url}: HTTP {response.status_code}")
    return response.content


def verify_sha256(content: bytes, expected: str) -> None:
    actual = sha256_hex(content)
    if actual != expected:
        raise UbigeoSourceError(
            f"El SHA-256 de la fuente no coincide: esperado {expected}, obtenido {actual}"
        )


def _clean_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def _line_ending(content: bytes) -> str:
    crlf = content.count(b"\r\n")
    lf = content.count(b"\n") - crlf
    if crlf and not lf:
        return "CRLF"
    if lf and not crlf:
        return "LF"
    return "mixto" if crlf and lf else "sin saltos de línea"


def transform(content: bytes) -> UbigeoDataset:
    """Pasos 2 a 5 de la sección 4.8. Lanza UbigeoSourceError ante cualquier dato inesperado."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UbigeoSourceError("La fuente no está en UTF-8") from exc
    if text.startswith("﻿"):
        raise UbigeoSourceError("La fuente tiene BOM; se esperaba UTF-8 sin BOM")

    dataset = UbigeoDataset(line_ending=_line_ending(content))
    # Con un parser de CSV: dos registros tienen un salto de línea dentro de un campo entre comillas.
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [column for column in (CODE_COLUMN, *NAME_COLUMNS) if column not in (reader.fieldnames or [])]
    if missing:
        raise UbigeoSourceError(f"Faltan columnas en la fuente: {', '.join(missing)}")

    seen: set[str] = set()
    for row in reader:
        dataset.read += 1
        code = row[CODE_COLUMN]  # siempre texto: conserva los ceros a la izquierda
        if any("\n" in (value or "") or "\r" in (value or "") for value in row.values()):
            dataset.multiline.append(code)
        if not DISTRICT_CODE.fullmatch(code):
            dataset.excluded.append(
                {
                    "inei": code,
                    "reniec": row.get("reniec") or "",
                    **{column: _clean_name(row[column] or "") for column in NAME_COLUMNS},
                }
            )
            continue

        names = {column: _clean_name(row[column] or "") for column in NAME_COLUMNS}
        for column, name in names.items():
            if not name:
                raise UbigeoSourceError(f"Nombre vacío en {column} del ubigeo {code}")
            if len(name) > MAX_NAME_LENGTHS[column]:
                raise UbigeoSourceError(
                    f"El nombre de {column} del ubigeo {code} supera {MAX_NAME_LENGTHS[column]} caracteres"
                )
        if code in seen:
            raise UbigeoSourceError(f"Código de ubigeo repetido: {code}")
        seen.add(code)

        for registry, key, name, label in (
            (dataset.departments, code[:2], names["departamento"], "departamento"),
            (dataset.provinces, code[:4], names["provincia"], "provincia"),
        ):
            previous = registry.setdefault(key, name)
            if previous != name:
                raise UbigeoSourceError(f"El {label} {key} tiene dos nombres: {previous!r} y {name!r}")

        dataset.districts.append((code, names["departamento"], names["provincia"], names["distrito"]))

    dataset.districts.sort()
    return dataset


def check_counts(dataset: UbigeoDataset, expected: ExpectedCounts = EXPECTED_COUNTS) -> None:
    actual = dataset.counts()
    if actual != expected:
        raise UbigeoSourceError(f"Los conteos no coinciden: esperado {expected}, obtenido {actual}")


def render_csv(dataset: UbigeoDataset) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(OUTPUT_HEADER)
    writer.writerows(dataset.districts)
    return buffer.getvalue().encode("utf-8")


def _names_with_accents(dataset: UbigeoDataset) -> list[str]:
    """Nombres con letras no ASCII distintas de la Ñ (se conservan tal como vienen en la fuente)."""
    found = []
    for code, department, province, district in dataset.districts:
        for label, name in (("departamento", department), ("provincia", province), ("distrito", district)):
            if any(ord(char) > 127 and char != "Ñ" for char in name):
                found.append(f"`{code}` ({label}): {name}")
    return found


def render_source_doc(
    dataset: UbigeoDataset,
    *,
    source_sha256: str,
    output_sha256: str,
    license_text: str,
    license_sha256: str,
    downloaded_on: date,
) -> str:
    counts = dataset.counts()
    excluded_rows = "\n".join(
        f"| `{row['inei']}` | `{row['reniec']}` | {row['departamento']} | {row['provincia']} | {row['distrito']} |"
        for row in dataset.excluded
    )
    accents = _names_with_accents(dataset)
    accents_text = "; ".join(accents) if accents else "ninguno"
    multiline = ", ".join(f"`{code}`" for code in dataset.multiline) or "ninguno"
    lengths = ", ".join(
        f"{column} {max(len(row[index]) for row in dataset.districts)}"
        for index, column in enumerate(NAME_COLUMNS, start=1)
    )
    return f"""# Fuente del catálogo Ubigeo INEI

Generado por `scripts/build_ubigeo_csv.py` (fase B1 del BackOffice; sección 4.8 de
`requerimientos/BACKOFFICE_DEALERS.spec.md`). No editar a mano: si hace falta un distrito nuevo, se agrega con una
migración nueva de Alembic (ver "Limitación y mantenimiento").

## Fuente

| Dato | Valor |
|---|---|
| Repositorio | `{SOURCE_REPOSITORY}` (GitHub). Dataset **comunitario**, no publicado por el INEI |
| Archivo | `{SOURCE_FILE}` |
| URL fijada al commit | {SOURCE_URL} |
| Commit | `{SOURCE_COMMIT}` ({SOURCE_COMMIT_DATE}) |
| Fecha de descarga | {downloaded_on:%d/%m/%Y} |
| SHA-256 del archivo original | `{source_sha256}` |
| SHA-256 de `ubigeo_inei.csv` | `{output_sha256}` |
| SHA-256 de `LICENSE` (mismo commit) | `{license_sha256}` |
| Licencia | MIT (texto completo al final) |

Formato observado del original: CSV UTF-8 sin BOM, con encabezado, fin de línea {dataset.line_ending}. Registros con un
salto de línea dentro de un campo entre comillas: {multiline}.

## Transformación

1. Descarga de la URL fijada y verificación del SHA-256 (si no coincide, el script se detiene).
2. Lectura con `csv.DictReader` (UTF-8). El código `inei` se trata siempre como texto (conserva los ceros a la
   izquierda). Se usan solo las columnas `inei` (→ `ubigeo`), `departamento`, `provincia` y `distrito`; las demás se
   descartan.
3. Se excluyen los registros cuyo `inei` no tiene 6 dígitos (ver "Registro excluido").
4. Trim y normalización Unicode NFC de los nombres. Los nombres se conservan tal como vienen en la fuente (mayúsculas;
   con tilde o Ñ solo donde la fuente la trae). Nombres con letras no ASCII distintas de la Ñ: {accents_text}.
5. Validaciones: códigos únicos; un solo nombre por departamento (2 dígitos) y por provincia (4 dígitos); nombres no
   vacíos y dentro del largo de las columnas (máximo observado: {lengths}).
6. Salida `ubigeo_inei.csv`: UTF-8 sin BOM, fin de línea LF, encabezado `{",".join(OUTPUT_HEADER)}`, ordenado por
   `ubigeo`. Departamentos y provincias se derivan de los distritos: código = primeros 2 o 4 dígitos; nombre = columna
   `departamento` o `provincia`.

## Conteos

| Concepto | Cantidad |
|---|---|
| Registros leídos | {counts.read} |
| Registros excluidos | {counts.excluded} |
| Distritos | {counts.districts} |
| Provincias | {counts.provinces} |
| Departamentos | {counts.departments} |

## Registro excluido (faltante conocido)

| `inei` | `reniec` | Departamento | Provincia | Distrito |
|---|---|---|---|---|
{excluded_rows}

Su código INEI **no se inventa**. Si un Dealer está en ese distrito, el responsable confirma el código INEI con una
fuente oficial (INEI o la ley de creación del distrito) y se agrega con una migración nueva.

## Limitación y mantenimiento

El archivo no cambia desde agosto de 2021 (al 15/11/2026, el commit fijado es el único que lo modifica), así que puede
faltar un distrito creado después. Si un Dealer está en un distrito que no aparece: el responsable confirma el código
INEI con una fuente oficial y se agrega con una migración nueva de Alembic. No hay actualización automática. Revisión
sugerida: una vez al año.

## Licencia

Texto completo del archivo `LICENSE` del repositorio en el commit fijado (la licencia exige conservar el aviso de
copyright):

```text
{license_text.rstrip()}
```
"""


def build(
    *,
    fetch: Callable[[str], bytes] = download,
    output_csv: Path = OUTPUT_CSV,
    output_doc: Path = OUTPUT_DOC,
    source_sha256: str = SOURCE_SHA256,
    expected: ExpectedCounts = EXPECTED_COUNTS,
    today: date | None = None,
) -> UbigeoDataset:
    """Descarga, verifica, transforma y escribe los dos archivos. Si algo falla, no escribe nada."""
    content = fetch(SOURCE_URL)
    verify_sha256(content, source_sha256)
    license_content = fetch(LICENSE_URL)
    try:
        license_text = license_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UbigeoSourceError("El archivo LICENSE no está en UTF-8") from exc
    if "MIT License" not in license_text:
        raise UbigeoSourceError("El archivo LICENSE del commit fijado no es la licencia MIT esperada")

    dataset = transform(content)
    check_counts(dataset, expected)

    csv_bytes = render_csv(dataset)
    doc = render_source_doc(
        dataset,
        source_sha256=sha256_hex(content),
        output_sha256=sha256_hex(csv_bytes),
        license_text=license_text.replace("\r\n", "\n"),
        license_sha256=sha256_hex(license_content),
        downloaded_on=today or datetime.now(ZoneInfo("America/Lima")).date(),
    )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_csv.write_bytes(csv_bytes)
    output_doc.write_bytes(doc.encode("utf-8"))
    return dataset


def main() -> int:
    try:
        dataset = build()
    except UbigeoSourceError as exc:
        print(f"Error: {exc}. No se escribió ningún archivo; avisar al responsable.", file=sys.stderr)
        return 1
    counts = dataset.counts()
    print(
        f"Leídos: {counts.read} · Excluidos: {counts.excluded} · Distritos: {counts.districts} · "
        f"Provincias: {counts.provinces} · Departamentos: {counts.departments}"
    )
    print(f"Escrito: {OUTPUT_CSV.relative_to(PROJECT_ROOT)} y {OUTPUT_DOC.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
