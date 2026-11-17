"""Exportación de Dealers a Excel .xlsx (BACKOFFICE_DEALERS.spec.md, sección 6.6, `GET /dealers/export`).

- Hoja `Dealers`; encabezado en negrita, fila congelada y autofiltro; anchos de columna ajustados.
- RUC, ubigeo y teléfono como **texto** (conservan los ceros). Fechas como fecha-hora de Excel en hora de Lima
  (`dd/mm/yyyy hh:mm`).
- **Inyección de fórmulas:** toda celda de texto se escribe como cadena literal (tipo `s`), nunca como fórmula, aunque
  empiece con `=`, `+`, `-` o `@`.

`build_dealers_workbook` es síncrono (openpyxl): el endpoint lo corre en un hilo aparte para no bloquear el event loop.
"""

import io
import logging
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models.audit_log import AuditAction
from app.schemas.backoffice.common import UserRef
from app.schemas.backoffice.dealers import DealerFilterQuery
from app.services.backoffice.audit import AuditContext, record_audit
from app.services.backoffice.dealers import DealerRecord, all_dealers

logger = logging.getLogger(__name__)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_TITLE = "Dealers"
DATETIME_FORMAT = "dd/mm/yyyy hh:mm"
HEADERS = (
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
)
# Ancho de las columnas de fecha (dd/mm/yyyy hh:mm) y límites del ajuste por contenido.
_DATETIME_WIDTH = 17
_MAX_WIDTH = 60


def export_filename(now: datetime, zone: ZoneInfo) -> str:
    """`dealers_AAAAMMDD_HHMM.xlsx` con la hora de Lima."""
    return f"dealers_{now.astimezone(zone):%Y%m%d_%H%M}.xlsx"


def _excel_datetime(value: datetime, zone: ZoneInfo) -> datetime:
    # Excel no guarda zona horaria: la hora de Lima, sin tzinfo.
    return value.astimezone(zone).replace(tzinfo=None)


def _username(refs: dict[uuid.UUID, UserRef], user_id: uuid.UUID | None) -> str | None:
    ref = refs.get(user_id) if user_id else None
    return ref.username if ref else None


def build_dealers_workbook(
    records: list[DealerRecord], refs: dict[uuid.UUID, UserRef], *, zone: ZoneInfo
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = SHEET_TITLE
    widths = [len(header) + 2 for header in HEADERS]

    def write_text(row: int, column: int, value: str | None) -> None:
        if value is None:
            return
        # openpyxl no admite caracteres de control en una celda: se quitan en lugar de fallar la exportación.
        text = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell = sheet.cell(row=row, column=column, value=text)
        # Cadena literal: openpyxl tomaría "=..." como fórmula.
        cell.data_type = "s"
        widths[column - 1] = max(widths[column - 1], min(len(text) + 2, _MAX_WIDTH))

    def write_datetime(row: int, column: int, value: datetime) -> None:
        cell = sheet.cell(row=row, column=column, value=_excel_datetime(value, zone))
        cell.number_format = DATETIME_FORMAT
        widths[column - 1] = max(widths[column - 1], _DATETIME_WIDTH)

    bold = Font(bold=True)
    for column, header in enumerate(HEADERS, start=1):
        write_text(1, column, header)
        sheet.cell(row=1, column=column).font = bold

    for row, record in enumerate(records, start=2):
        dealer = record.dealer
        texts = {
            1: dealer.dealer_code,
            2: dealer.business_name,
            3: dealer.ruc,
            4: dealer.address,
            5: record.department.name,
            6: record.province.name,
            7: record.district.name,
            8: dealer.district_ubigeo,
            9: dealer.contact_email,
            10: dealer.contact_phone,
            11: dealer.contact_name,
            13: _username(refs, dealer.created_by),
            15: _username(refs, dealer.updated_by),
        }
        for column, value in texts.items():
            write_text(row, column, value)
        write_datetime(row, 12, dealer.created_at)
        write_datetime(row, 14, dealer.updated_at)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{max(len(records) + 1, 1)}"
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def applied_filters(query: DealerFilterQuery) -> dict[str, str]:
    """Filtros con valor, en camelCase (`details.filters` de DEALERS_EXPORTED)."""
    values = {
        "q": query.q,
        "departmentCode": query.department_code,
        "provinceCode": query.province_code,
        "districtUbigeo": query.district_ubigeo,
    }
    return {key: value for key, value in values.items() if value}


async def export_dealers(
    db: AsyncSession,
    query: DealerFilterQuery,
    *,
    actor_user_id: uuid.UUID,
    context: AuditContext,
    zone: ZoneInfo,
) -> tuple[bytes, str]:
    """Dealers vigentes con los filtros y el orden de la lista, sin paginación. Registra DEALERS_EXPORTED
    (`{"filters", "rows"}`) y devuelve (contenido .xlsx, nombre del archivo). Hace commit."""
    now = datetime.now(UTC)
    records, refs = await all_dealers(db, query)
    content = await run_in_threadpool(build_dealers_workbook, records, refs, zone=zone)
    record_audit(
        db,
        AuditAction.DEALERS_EXPORTED,
        context=context,
        actor_user_id=actor_user_id,
        details={"filters": applied_filters(query), "rows": len(records)},
    )
    await db.commit()
    logger.info("Dealers exportados a Excel", extra={"user_id": str(actor_user_id), "rows": len(records)})
    return content, export_filename(now, zone)
