"""Búsqueda y paginación de las listas del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.1).

`q` se busca con `ILIKE '%q%'` con `%`, `_` y `\\` escapados: el texto escrito se busca literal, nunca como patrón.
"""

from typing import Any

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

LIKE_ESCAPE = "\\"


def like_pattern(text: str) -> str:
    """Patrón `%texto%` con los comodines de LIKE escapados (`50%` busca "50%", no "50" seguido de cualquier cosa)."""
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def contains_any(text: str, *columns: ColumnElement[Any]) -> ColumnElement[bool]:
    """Alguna de las columnas contiene `text`, sin distinguir mayúsculas."""
    pattern = like_pattern(text)
    return or_(*(column.ilike(pattern, escape=LIKE_ESCAPE) for column in columns))


async def paginate(
    db: AsyncSession, statement: Select[Any], *, page: int, page_size: int
) -> tuple[list[Row[Any]], int]:
    """Filas de la página pedida y el total de filas de la consulta (sin paginar)."""
    total = await db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    rows = (await db.execute(statement.limit(page_size).offset((page - 1) * page_size))).all()
    return list(rows), total or 0
