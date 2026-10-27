from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter(tags=["health"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/health")
async def health(session: SessionDep) -> JSONResponse:
    """Verifica que la API esté arriba y que la base de datos responda."""
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return JSONResponse(
            {"status": "error", "database": "unreachable"}, status_code=503
        )
    return JSONResponse({"status": "ok", "database": "ok"})
