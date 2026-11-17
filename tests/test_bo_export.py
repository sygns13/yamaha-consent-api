"""Exportación de Dealers a Excel (BACKOFFICE_DEALERS.spec.md v1.2, sección 6.6; fase B5): criterio B5.8."""

import io
import re
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from app.models import AuditAction
from app.services.backoffice.exports import HEADERS, export_filename
from tests.bo_helpers import (
    BO,
    BoUser,
    Sessions,
    audit_rows,
    create_dealer_row,
    logged_client,
    make_ruc,
)

EXPORT = f"{BO}/dealers/export"
LIMA = ZoneInfo("America/Lima")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ClientFactory = Callable[[], httpx.AsyncClient]

SPEC_COLUMNS = [
    "Código Dealer",
    "Razón social",
    "RUC",
    "Dirección",
    "Departamento",
    "Provincia",
    "Distrito",
    "Ubigeo",
    "Correo de contacto",
    "Teléfono de contacto",
    "Persona de contacto",
    "Creado el",
    "Creado por",
    "Actualizado el",
    "Actualizado por",
]


@pytest.fixture
async def operator_client(make_bo_client: ClientFactory, operator_user: BoUser) -> httpx.AsyncClient:
    return await logged_client(make_bo_client, operator_user)


def open_sheet(content: bytes) -> Worksheet:
    workbook = load_workbook(io.BytesIO(content))
    assert workbook.sheetnames == ["Dealers"]
    sheet = workbook["Dealers"]
    assert isinstance(sheet, Worksheet)
    return sheet


def data_rows(sheet: Worksheet) -> list[tuple[Any, ...]]:
    return [tuple(cell.value for cell in row) for row in sheet.iter_rows(min_row=2)]


@pytest.fixture
async def exported_dealers(session_factory: Sessions, operator_user: BoUser) -> dict[str, uuid.UUID]:
    created = datetime(2026, 11, 15, 15, 35, tzinfo=UTC)  # 10:35 en Lima
    return {
        "CL0001": await create_dealer_row(
            session_factory,
            dealer_code="CL0001",
            business_name="=1+1",
            ruc="20100047218",
            address="+SUM(A1)",
            district_ubigeo="150101",
            contact_email="contacto@abcmotors.pe",
            contact_phone="014567890",
            contact_name="@María López",
            created_by=operator_user.id,
            updated_by=operator_user.id,
            created_at=created,
            updated_at=created + timedelta(days=1, hours=1),
        ),
        "CA0001": await create_dealer_row(
            session_factory,
            dealer_code="CA0001",
            business_name=None,
            ruc=make_ruc(5),
            address="-10 Av. Callao",
            district_ubigeo="070101",
            contact_email="callao@motos.pe",
            created_at=created,
            updated_at=created,
        ),
        "BORRADO": await create_dealer_row(session_factory, dealer_code="BORRADO", is_deleted=1),
    }


async def test_export_file(
    operator_client: httpx.AsyncClient,
    operator_user: BoUser,
    exported_dealers: dict[str, uuid.UUID],
    session_factory: Sessions,
) -> None:
    before = datetime.now(UTC)
    response = await operator_client.get(EXPORT)
    after = datetime.now(UTC)

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX
    assert response.headers["cache-control"] == "no-store"
    match = re.fullmatch(
        r'attachment; filename="(dealers_\d{8}_\d{4}\.xlsx)"', response.headers["content-disposition"]
    )
    assert match is not None
    assert match.group(1) in {export_filename(before, LIMA), export_filename(after, LIMA)}

    sheet = open_sheet(response.content)
    assert [cell.value for cell in sheet[1]] == SPEC_COLUMNS == list(HEADERS)
    assert all(cell.font.bold for cell in sheet[1])
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:O3"

    rows = data_rows(sheet)
    # Orden de la lista (dealerCode); sin el eliminado.
    assert [row[0] for row in rows] == ["CA0001", "CL0001"]
    assert rows[1] == (
        "CL0001",
        "=1+1",
        "20100047218",
        "+SUM(A1)",
        "LIMA",
        "LIMA",
        "LIMA",
        "150101",
        "contacto@abcmotors.pe",
        "014567890",
        "@María López",
        datetime(2026, 11, 15, 10, 35),
        operator_user.username,
        datetime(2026, 11, 16, 11, 35),
        operator_user.username,
    )
    assert rows[0][:4] == ("CA0001", None, make_ruc(5), "-10 Av. Callao")
    assert rows[0][7] == "070101" and rows[0][12] is None


async def test_text_is_never_a_formula(
    operator_client: httpx.AsyncClient, exported_dealers: dict[str, uuid.UUID]
) -> None:
    response = await operator_client.get(EXPORT)
    sheet = open_sheet(response.content)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            # Texto (s) o fecha (d); nunca fórmula (f).
            if cell.value is not None:
                assert cell.data_type in ("s", "d"), (cell.coordinate, cell.data_type)
    # RUC, ubigeo y teléfono como texto (conservan los ceros).
    cl0001 = sheet[3]
    assert [cl0001[index].data_type for index in (2, 7, 9)] == ["s", "s", "s"]
    assert sheet["H2"].value == "070101"
    # Fechas como fecha-hora de Excel con el formato pedido.
    for coordinate in ("L3", "N3"):
        assert isinstance(sheet[coordinate].value, datetime)
        assert sheet[coordinate].number_format == "dd/mm/yyyy hh:mm"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>" not in xml and "<f " not in xml
    assert "=1+1" in xml


async def test_export_applies_filters_and_order(
    operator_client: httpx.AsyncClient, exported_dealers: dict[str, uuid.UUID], session_factory: Sessions
) -> None:
    response = await operator_client.get(EXPORT, params={"departmentCode": "07"})
    assert [row[0] for row in data_rows(open_sheet(response.content))] == ["CA0001"]

    response = await operator_client.get(EXPORT, params={"q": "abcmotors"})
    assert [row[0] for row in data_rows(open_sheet(response.content))] == []  # el correo no está en `q`

    response = await operator_client.get(EXPORT, params={"sortOrder": "desc", "page": 2, "pageSize": 10})
    # Sin paginación: page y pageSize no existen en la exportación (se ignoran).
    assert [row[0] for row in data_rows(open_sheet(response.content))] == ["CL0001", "CA0001"]

    response = await operator_client.get(EXPORT, params={"provinceCode": "1501", "q": "cl00"})
    assert [row[0] for row in data_rows(open_sheet(response.content))] == ["CL0001"]


async def test_export_audit(
    operator_client: httpx.AsyncClient,
    operator_user: BoUser,
    exported_dealers: dict[str, uuid.UUID],
    session_factory: Sessions,
) -> None:
    await operator_client.get(EXPORT, params={"departmentCode": "07", "q": "  ca  ", "sortBy": "ruc"})
    await operator_client.get(EXPORT)

    first, second = await audit_rows(session_factory, AuditAction.DEALERS_EXPORTED)
    assert first.actor_user_id == operator_user.id
    assert first.entity_type is None and first.entity_id is None
    assert first.details == {"filters": {"q": "ca", "departmentCode": "07"}, "rows": 1}
    assert second.details == {"filters": {}, "rows": 2}


async def test_empty_export(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    response = await operator_client.get(EXPORT)
    assert response.status_code == 200
    sheet = open_sheet(response.content)
    assert sheet.max_row == 1 and sheet.auto_filter.ref == "A1:O1"
    (entry,) = await audit_rows(session_factory, AuditAction.DEALERS_EXPORTED)
    assert entry.details == {"filters": {}, "rows": 0}


async def test_control_characters_are_removed(
    operator_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create_dealer_row(session_factory, dealer_code="CL0001", address="Av.\x01 Uno\x1f")
    response = await operator_client.get(EXPORT)
    assert response.status_code == 200
    assert data_rows(open_sheet(response.content))[0][3] == "Av. Uno"


async def test_column_widths_fit_the_content(
    operator_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    await create_dealer_row(session_factory, dealer_code="CL0001", address="A" * 250)
    sheet = open_sheet((await operator_client.get(EXPORT)).content)
    assert sheet.column_dimensions["D"].width == 60  # máximo
    assert sheet.column_dimensions["A"].width >= len("Código Dealer")
    assert sheet.column_dimensions["L"].width >= 16  # dd/mm/yyyy hh:mm


async def test_export_query_validation(operator_client: httpx.AsyncClient, session_factory: Sessions) -> None:
    response = await operator_client.get(EXPORT, params={"departmentCode": "1"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "departmentCode"]
    assert await audit_rows(session_factory, AuditAction.DEALERS_EXPORTED) == []


def test_export_filename_uses_lima_time() -> None:
    # 2026-11-16 03:05 UTC = 2026-11-15 22:05 en Lima.
    assert export_filename(datetime(2026, 11, 16, 3, 5, tzinfo=UTC), LIMA) == "dealers_20261115_2205.xlsx"
