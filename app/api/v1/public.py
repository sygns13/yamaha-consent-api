"""API pública para la landing (M4). Sin API Key; las respuestas nunca incluyen datos personales."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.schemas.public import (
    ConsentLinkInfo,
    ConsentSubmitRequest,
    ConsentSubmitResponse,
    LinkGoneResponse,
    LinkNotFoundResponse,
    LinkStatus,
)
from app.services.consent_links import (
    LinkNotFoundError,
    LinkUnavailableError,
    SubmissionEvidence,
    get_link_status,
    submit_consent,
)
from app.services.kizuna_notifications import dispatch_notifications
from app.utils.client_ip import client_ip

router = APIRouter(prefix="/api/v1/public", tags=["public"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

LINK_NOT_FOUND = "El enlace no existe"
LINK_GONE = {
    LinkStatus.EXPIRED: "El enlace venció",
    LinkStatus.USED: "El enlace ya fue utilizado",
}

_NOT_FOUND_RESPONSE = {
    status.HTTP_404_NOT_FOUND: {
        "model": LinkNotFoundResponse,
        "description": "No existe un enlace con ese token",
    }
}


@router.get(
    "/consent-links/{token}",
    response_model=ConsentLinkInfo,
    summary="Validar el enlace de la landing",
    responses=_NOT_FOUND_RESPONSE,
)
async def read_consent_link(token: str, session: SessionDep, settings: SettingsDep) -> ConsentLinkInfo:
    """`link_status`: VALID, EXPIRED o USED (un enlace vencido o usado responde 200; la landing lo trata)."""
    link_status = await get_link_status(session, token)
    if link_status is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=LINK_NOT_FOUND)
    return ConsentLinkInfo(link_status=link_status, privacy_policy_url=settings.privacy_policy_url or None)


@router.post(
    "/consent-links/{token}/submit",
    response_model=ConsentSubmitResponse,
    summary="Registrar las respuestas del cliente",
    responses={
        **_NOT_FOUND_RESPONSE,
        status.HTTP_410_GONE: {"model": LinkGoneResponse, "description": "El enlace venció o ya fue usado"},
    },
)
async def submit_consent_link(
    token: str,
    body: ConsentSubmitRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
) -> ConsentSubmitResponse | JSONResponse:
    """Con `personal_data` en false se registra el rechazo (RN-01): no es un error, responde 200 NO_ACEPTADO."""
    evidence = SubmissionEvidence(
        ip_address=client_ip(
            request.headers.get("x-forwarded-for"), request.client.host if request.client else None
        ),
        user_agent=request.headers.get("user-agent"),
    )
    try:
        outcome = await submit_consent(session, token, body, evidence=evidence, settings=settings)
    except LinkNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=LINK_NOT_FOUND) from None
    except LinkUnavailableError as exc:
        gone = LinkGoneResponse(detail=LINK_GONE[exc.link_status], link_status=exc.link_status.value)
        return JSONResponse(status_code=status.HTTP_410_GONE, content=gone.model_dump())

    # Paso 8: después del commit (submit_consent ya terminó su transacción) y después de responder.
    background_tasks.add_task(dispatch_notifications, outcome.notification_ids)
    return ConsentSubmitResponse(status=outcome.status.value)
