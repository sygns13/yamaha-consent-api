"""Convenciones de los schemas del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.1).

JSON en camelCase (`firstNames`), Python en snake_case (`first_names`). Los requests rechazan campos desconocidos
(`extra="forbid"` → 422).

Listas (sección 6.1): query `page` (≥ 1, default 1), `pageSize` (10, 20, 50 o 100; default 20), `sortBy`, `sortOrder`
(`asc` | `desc`) y búsqueda `q` (trim, máximo 100); respuesta `{"items", "total", "page", "pageSize"}`.
"""

import uuid
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel


class BackofficeRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        extra="forbid",
    )


class BackofficeResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class ErrorResponse(BackofficeResponse):
    """Cuerpo de un BackofficeError (sección 5.8). `field` solo si el error es de un campo (en camelCase)."""

    detail: str
    code: str
    field: str | None = None


def error_responses(*codes: int) -> dict[int | str, dict[str, object]]:
    """`responses=` de OpenAPI: el cuerpo `ErrorResponse` para cada código."""
    return {code: {"model": ErrorResponse} for code in codes}


# --- Listas (sección 6.1) ---

PAGE_SIZES = (10, 20, 50, 100)
SEARCH_MAX_LENGTH = 100


def _check_page_size(value: int) -> int:
    # Literal[10, 20, 50, 100] no acepta el texto "20" que llega en la query: se valida aquí.
    if value not in PAGE_SIZES:
        raise ValueError("Debe ser uno de estos valores: 10, 20, 50 o 100")
    return value


def _empty_to_none(value: str | None) -> str | None:
    return value or None


PageSize = Annotated[int, AfterValidator(_check_page_size)]
SortOrder = Literal["asc", "desc"]
# Trim y máximo 100; vacía = sin búsqueda.
SearchText = Annotated[
    str | None,
    StringConstraints(strip_whitespace=True, max_length=SEARCH_MAX_LENGTH),
    AfterValidator(_empty_to_none),
]


class QueryModel(BaseModel):
    """Parámetros de query en camelCase. Los parámetros desconocidos se ignoran (no es un body)."""

    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, validate_by_alias=True)


class PageQuery(QueryModel):
    page: int = Field(default=1, ge=1)
    page_size: PageSize = 20


class ListQuery(PageQuery):
    """Paginación + sentido del orden (cada lista define su `sortBy`)."""

    sort_order: SortOrder = "asc"


class Page[ItemT](BackofficeResponse):
    items: list[ItemT]
    total: int
    page: int
    page_size: int


class UserRef(BackofficeResponse):
    """Referencia a un usuario en respuestas (sección 6.1). `fullName` = nombres + apellidos."""

    id: uuid.UUID
    username: str
    full_name: str
