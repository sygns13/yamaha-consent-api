"""Callback de estado de Twilio (M3): application/x-www-form-urlencoded con los nombres de campo de Twilio."""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


class TwilioStatusCallback(BaseModel):
    """Campos que usa el webhook. Twilio envía más (To, From, AccountSid...): se guardan en raw_payload."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    message_sid: str = Field(alias="MessageSid", min_length=1, max_length=64)
    message_status: str = Field(alias="MessageStatus", min_length=1)
    error_code: Annotated[str | None, BeforeValidator(_empty_to_none)] = Field(
        default=None, alias="ErrorCode", max_length=20
    )
    error_message: Annotated[str | None, BeforeValidator(_empty_to_none)] = Field(
        default=None, alias="ErrorMessage"
    )
