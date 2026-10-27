"""Webhook de estados de Twilio (M3). No usa API Key: lo protege la firma X-Twilio-Signature."""

import logging
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.integrations.twilio_client import is_valid_twilio_signature
from app.schemas.twilio import TwilioStatusCallback
from app.services.whatsapp_status import record_status_callback

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

SIGNATURE_HEADER = "X-Twilio-Signature"
MISSING_SIGNATURE = "Falta la firma de Twilio"
INVALID_SIGNATURE = "Firma de Twilio inválida"

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post(
    "/twilio/status",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Callback de estado de Twilio",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Falta la firma X-Twilio-Signature o es inválida"},
    },
)
async def twilio_status_callback(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    """Guarda el evento y actualiza el estado del mensaje. Responde 204 también ante callbacks repetidos, de
    mensajes desconocidos o con estados desconocidos (Twilio no tiene nada que corregir en esos casos).
    """
    form = await request.form()
    # La firma se calcula sobre todos los parámetros que envió Twilio.
    params = {key: str(value) for key, value in form.items()}

    if settings.twilio_signature_validation_enabled:
        signature = request.headers.get(SIGNATURE_HEADER, "")
        if not signature:
            _reject(MISSING_SIGNATURE)
        # Twilio firma la URL a la que llamó: la configurada como status_callback. Detrás de Cloud Run la app ve
        # la petición como http interno, por eso la URL de la petición es solo la alternativa.
        urls = (settings.twilio_status_callback_url, str(request.url))
        if not is_valid_twilio_signature(
            auth_token=settings.twilio_auth_token.get_secret_value(),
            signature=signature,
            urls=urls,
            params=params,
        ):
            _reject(INVALID_SIGNATURE)

    try:
        callback = TwilioStatusCallback.model_validate(params)
    except ValidationError as exc:
        # Se responde con el mismo formato y en español (validation_errors.py), con loc ["body", campo].
        raise RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors()]
        ) from None

    await record_status_callback(session, callback, raw_payload=params)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _reject(detail: str) -> NoReturn:
    # Ni la firma ni el payload (trae el teléfono) van al log.
    logger.warning("Callback de Twilio rechazado: %s", detail)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
