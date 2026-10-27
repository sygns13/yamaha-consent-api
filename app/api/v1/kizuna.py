"""Endpoints para KIZUNA (M2). Todos requieren API Key."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import ApiKeyDep
from app.db.session import get_session
from app.integrations.whatsapp import WhatsAppSender, get_whatsapp_sender
from app.schemas import (
    ConsentConflictResponse,
    ConsentCreateRequest,
    ConsentCreateResponse,
    ConsentDetail,
    ConsentListQuery,
    WhatsAppSendStatus,
)
from app.services.consent_queries import get_consent, list_consents
from app.services.consents import RegistrationResult, TransactionAlreadyRejectedError, register_consent
from app.services.kizuna_notifications import KizunaNotifier, get_kizuna_notifier

router = APIRouter(prefix="/api/v1", tags=["kizuna"], dependencies=[ApiKeyDep])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SenderDep = Annotated[WhatsAppSender, Depends(get_whatsapp_sender)]
NotifierDep = Annotated[KizunaNotifier, Depends(get_kizuna_notifier)]

REJECTED_DETAIL = "La transacción ya existe y fue rechazada (NO_ACEPTADO); no se modificó ningún dato"
NOT_FOUND_DETAIL = "No existe un registro con ese ID de transacción"


class NotFoundResponse(BaseModel):
    detail: str


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
    background_tasks: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    sender: SenderDep,
    notifier: NotifierDep,
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
    if outcome.notification_ids:
        # Copia vinculada que nació con respuesta: se notifica a KIZUNA después del commit (M5).
        background_tasks.add_task(notifier.dispatch, outcome.notification_ids)

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


@router.get(
    "/consents/{transaction_id}",
    response_model=ConsentDetail,
    summary="Consultar un registro por ID de transacción",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": NotFoundResponse, "description": "No existe el registro"}
    },
)
async def read_consent(transaction_id: str, session: SessionDep) -> ConsentDetail:
    """Incluye los registros borrados lógicamente y las copias vinculadas (RN-04). No devuelve nombre ni teléfono."""
    consent = await get_consent(session, transaction_id)
    if consent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return ConsentDetail.from_consent(consent)


@router.get(
    "/consents",
    response_model=list[ConsentDetail],
    summary="Listar los registros de un documento",
)
async def search_consents(
    query: Annotated[ConsentListQuery, Query()], session: SessionDep
) -> list[ConsentDetail]:
    """Registros del documento (y del Dealer, opcional), del más reciente al más antiguo.

    Incluye los borrados lógicamente. Sin resultados → 200 con lista vacía. El documento se normaliza igual que al
    registrarlo (p. ej. "1234-5678" encuentra "12345678").
    """
    return [ConsentDetail.from_consent(consent) for consent in await list_consents(session, query)]
