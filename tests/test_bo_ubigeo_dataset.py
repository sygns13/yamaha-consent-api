"""Dataset Ubigeo (fase B1, sección 4.8 del spec del BackOffice): el CSV versionado y el script que lo genera.

Sin red: el script se prueba con una fuente de muestra (`fetch` simulado). El CSV real ya está versionado en
alembic/data/ y su contenido en la base se verifica en tests/test_bo_schema.py.
"""

import csv
import hashlib
import io
import re
from pathlib import Path

import pytest

from scripts import build_ubigeo_csv as builder
from scripts.build_ubigeo_csv import ExpectedCounts, UbigeoSourceError

DATA_DIR = Path(__file__).resolve().parents[1] / "alembic" / "data"
CSV_PATH = DATA_DIR / "ubigeo_inei.csv"
DOC_PATH = DATA_DIR / "UBIGEO_FUENTE.md"

MIT_TEXT = "MIT License\n\nCopyright (c) 2021 Jesus M. Castagnetto\n\nPermission is hereby granted, free of charge, ...\n"

# Fuente de muestra con lo que tiene la real: CRLF aquí (la real usa LF), un salto de línea dentro de un campo entre
# comillas, el registro sin código INEI, ceros a la izquierda, un nombre con espacios y una Ñ descompuesta (NFD).
SAMPLE = (
    "inei,reniec,departamento,provincia,distrito,capital\r\n"
    "150501,140501, LIMA ,CAN\u0303ETE,SAN VICENTE DE CAN\u0303ETE,X\r\n"
    '010519,010519,AMAZONAS,LUYA,LONYA CHICO,"LONYA\r\nCHICO"\r\n'
    "NA,170107,MOQUEGUA,MARISCAL NIETO,SAN ANTONIO,X\r\n"
    "010101,010101,AMAZONAS,CHACHAPOYAS,CHACHAPOYAS,X\r\n"
).encode()
SAMPLE_COUNTS = ExpectedCounts(read=4, excluded=1, districts=3, provinces=3, departments=2)


def sample_fetch(source: bytes = SAMPLE):
    def fetch(url: str) -> bytes:
        return {builder.SOURCE_URL: source, builder.LICENSE_URL: MIT_TEXT.encode()}[url]

    return fetch


# --- CSV versionado ---


def test_versioned_csv_format() -> None:
    content = CSV_PATH.read_bytes()
    assert not content.startswith(b"\xef\xbb\xbf")  # sin BOM
    assert b"\r" not in content  # fin de línea LF
    rows = list(csv.reader(io.StringIO(content.decode("utf-8"), newline="")))
    assert rows[0] == ["ubigeo", "departamento", "provincia", "distrito"]
    codes = [row[0] for row in rows[1:]]
    assert len(codes) == 1892
    assert codes == sorted(codes)
    assert len(set(codes)) == len(codes)
    assert all(re.fullmatch(r"[0-9]{6}", code) for code in codes)
    assert all(all(value == value.strip() and value for value in row) for row in rows[1:])
    assert ["150501", "LIMA", "CAÑETE", "SAN VICENTE DE CAÑETE"] in rows


def test_source_doc_matches_versioned_csv() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    csv_sha256 = hashlib.sha256(CSV_PATH.read_bytes()).hexdigest()
    assert builder.SOURCE_COMMIT in doc
    assert builder.SOURCE_URL in doc
    assert builder.SOURCE_SHA256 in doc  # SHA-256 del original
    assert f"SHA-256 de `ubigeo_inei.csv` | `{csv_sha256}`" in doc  # SHA-256 del CSV final
    assert "| `NA` | `170107` | MOQUEGUA | MARISCAL NIETO | SAN ANTONIO |" in doc  # registro excluido
    for count in ("| Registros leídos | 1893 |", "| Distritos | 1892 |", "| Provincias | 196 |"):
        assert count in doc
    # Texto completo de la licencia MIT, con el aviso de copyright.
    assert "MIT License" in doc
    assert "Copyright (c) 2021 Jesus M. Castagnetto" in doc
    assert "The above copyright notice and this permission notice shall be included in all" in doc
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in doc


# --- Transformación (pasos 2 a 6) ---


def test_transform_sample() -> None:
    dataset = builder.transform(SAMPLE)
    assert dataset.counts() == SAMPLE_COUNTS
    assert dataset.districts == [
        ("010101", "AMAZONAS", "CHACHAPOYAS", "CHACHAPOYAS"),
        ("010519", "AMAZONAS", "LUYA", "LONYA CHICO"),
        ("150501", "LIMA", "CAÑETE", "SAN VICENTE DE CAÑETE"),  # trim y NFC
    ]
    assert dataset.departments == {"15": "LIMA", "01": "AMAZONAS"}
    assert dataset.provinces == {"1505": "CAÑETE", "0105": "LUYA", "0101": "CHACHAPOYAS"}
    assert dataset.excluded == [
        {
            "inei": "NA",
            "reniec": "170107",
            "departamento": "MOQUEGUA",
            "provincia": "MARISCAL NIETO",
            "distrito": "SAN ANTONIO",
        }
    ]
    assert dataset.multiline == ["010519"]
    assert dataset.line_ending == "CRLF"


def test_render_csv_is_lf_without_bom_and_sorted() -> None:
    content = builder.render_csv(builder.transform(SAMPLE))
    assert (
        content
        == (
            "ubigeo,departamento,provincia,distrito\n"
            "010101,AMAZONAS,CHACHAPOYAS,CHACHAPOYAS\n"
            "010519,AMAZONAS,LUYA,LONYA CHICO\n"
            "150501,LIMA,CAÑETE,SAN VICENTE DE CAÑETE\n"
        ).encode()
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"\xef\xbb\xbfinei,departamento,provincia,distrito\n", "BOM"),
        (b"\xff\xfe", "UTF-8"),
        (b"ubigeo,departamento,provincia,distrito\n", "Faltan columnas"),
        (b"inei,departamento,provincia,distrito\n010101,A,B,C\n010101,A,B,D\n", "repetido"),
        (b"inei,departamento,provincia,distrito\n010101,A,B,C\n010201,OTRO,D,E\n", "dos nombres"),
        (b"inei,departamento,provincia,distrito\n010101,A,B,C\n010102,A,OTRA,E\n", "dos nombres"),
        (b"inei,departamento,provincia,distrito\n010101,A,  ,C\n", "vac"),
        (b"inei,departamento,provincia,distrito\n010101," + b"A" * 61 + b",B,C\n", "supera 60"),
    ],
)
def test_transform_rejects_unexpected_data(source: bytes, message: str) -> None:
    with pytest.raises(UbigeoSourceError, match=message):
        builder.transform(source)


# --- Ejecución completa (con fetch simulado) ---


def test_build_writes_both_files(tmp_path: Path) -> None:
    out_csv, out_doc = tmp_path / "ubigeo_inei.csv", tmp_path / "UBIGEO_FUENTE.md"
    builder.build(
        fetch=sample_fetch(),
        output_csv=out_csv,
        output_doc=out_doc,
        source_sha256=hashlib.sha256(SAMPLE).hexdigest(),
        expected=SAMPLE_COUNTS,
    )
    csv_bytes = out_csv.read_bytes()
    doc = out_doc.read_text(encoding="utf-8")
    assert csv_bytes.startswith(b"ubigeo,departamento,provincia,distrito\n")
    assert hashlib.sha256(csv_bytes).hexdigest() in doc
    assert hashlib.sha256(SAMPLE).hexdigest() in doc
    assert "| `NA` | `170107` | MOQUEGUA | MARISCAL NIETO | SAN ANTONIO |" in doc
    assert MIT_TEXT.strip() in doc


def test_build_stops_when_hash_does_not_match(tmp_path: Path) -> None:
    out_csv, out_doc = tmp_path / "ubigeo_inei.csv", tmp_path / "UBIGEO_FUENTE.md"
    with pytest.raises(UbigeoSourceError, match="SHA-256"):
        # El hash fijado del commit real no coincide con la muestra.
        builder.build(fetch=sample_fetch(), output_csv=out_csv, output_doc=out_doc, expected=SAMPLE_COUNTS)
    assert not out_csv.exists() and not out_doc.exists()


def test_build_stops_when_counts_do_not_match(tmp_path: Path) -> None:
    out_csv, out_doc = tmp_path / "ubigeo_inei.csv", tmp_path / "UBIGEO_FUENTE.md"
    with pytest.raises(UbigeoSourceError, match="conteos"):
        builder.build(
            fetch=sample_fetch(),
            output_csv=out_csv,
            output_doc=out_doc,
            source_sha256=hashlib.sha256(SAMPLE).hexdigest(),
            # Los conteos del commit real (1.893 / 1 / 1.892 / 196 / 25) no son los de la muestra.
        )
    assert not out_csv.exists() and not out_doc.exists()


def test_build_stops_when_license_is_not_mit(tmp_path: Path) -> None:
    def fetch(url: str) -> bytes:
        return SAMPLE if url == builder.SOURCE_URL else b"Apache License"

    with pytest.raises(UbigeoSourceError, match="MIT"):
        builder.build(
            fetch=fetch,
            output_csv=tmp_path / "a.csv",
            output_doc=tmp_path / "b.md",
            source_sha256=hashlib.sha256(SAMPLE).hexdigest(),
            expected=SAMPLE_COUNTS,
        )


def test_main_reports_error_in_spanish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_build() -> None:
        raise UbigeoSourceError("No se pudo descargar la fuente: HTTP 404")

    monkeypatch.setattr(builder, "build", failing_build)
    assert builder.main() == 1
    assert "No se escribió ningún archivo; avisar al responsable" in capsys.readouterr().err


def test_download_fails_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 404
        content = b""

    monkeypatch.setattr(builder.httpx, "get", lambda *args, **kwargs: Response())
    with pytest.raises(UbigeoSourceError, match="HTTP 404"):
        builder.download(builder.SOURCE_URL)
