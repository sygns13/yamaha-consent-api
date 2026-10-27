"""Endpoints para KIZUNA (M2). Todos requieren API Key."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import ApiKeyDep
from app.db.session import get_session
from app.integrations.whatsapp import WhatsAppSender, get_whatsapp_sender
from app.schemas import (
    ConsentConflictResponse,
    ConsentCreateRequest,
    ConsentCreateResponse,
    WhatsAppSendStatus,
)
from app.services.consents import RegistrationResult, TransactionAlreadyRejectedError, register_consent

router = APIRouter(prefix="/api/v1", tags=["kizuna"], dependencies=[ApiKeyDep])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SenderDep = Annotated[WhatsAppSender, Depends(get_whatsapp_sender)]

REJECTED_DETAIL = "La transacción ya existe y fue rechazada (NO_ACEPTADO); no se modificó ningún dato"


@router.post(
    "/consents",
    response_model=ConsentCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar o actualizar un cliente desde KIZUNA",
    responses={
        status.HTTP_200_OK: {
            "model": ConsentCreateResponse,
            "description": "UPDATED (RN-04): se actualizaron los datos del cliente; no se envía WhatsApp",
        },
        status.HTTP_201_CREATED: {"description": "CREATED: registro, enlace y WhatsApp nuevos"},
        status.HTTP_409_CONFLICT: {
            "model": ConsentConflictResponse,
            "description": "El transaction_id ya existe y está borrado lógicamente (NO_ACEPTADO)",
        },
    },
)
async def create_consent(
    body: ConsentCreateRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    sender: SenderDep,
) -> ConsentCreateResponse | JSONResponse:
    try:
        outcome = await register_consent(session, body, sender=sender, settings=settings)
    except TransactionAlreadyRejectedError as exc:
        conflict = ConsentConflictResponse(
            detail=REJECTED_DETAIL,
            transaction_id=exc.transaction_id,
            status=exc.status,
            is_deleted=exc.is_deleted,
        )
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=conflict.model_dump(mode="json"))

    if outcome.result is RegistrationResult.UPDATED:
        response.status_code = status.HTTP_200_OK

    whatsapp = None
    if outcome.whatsapp is not None:
        whatsapp = WhatsAppSendStatus(status=outcome.whatsapp.status, error_code=outcome.whatsapp.error_code)

    return ConsentCreateResponse(
        transaction_id=body.transaction_id,
        result=outcome.result.value,
        affected_transaction_id=outcome.consent.transaction_id,
        status=outcome.consent.status,
        whatsapp=whatsapp,
    )
