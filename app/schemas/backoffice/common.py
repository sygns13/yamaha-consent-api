"""Convenciones de los schemas del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6.1).

JSON en camelCase (`firstNames`), Python en snake_case (`first_names`). Los requests rechazan campos desconocidos
(`extra="forbid"` → 422).
"""

from pydantic import BaseModel, ConfigDict
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
