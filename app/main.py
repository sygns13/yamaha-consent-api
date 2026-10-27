from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import health, kizuna, public, webhooks
from app.core.config import get_settings
from app.core.http_errors import http_exception_handler, unhandled_exception_handler
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware
from app.core.security import add_cors, check_security_settings
from app.core.validation_errors import request_validation_exception_handler
from app.db.session import get_session_factory
from app.integrations.kizuna_client import check_kizuna_settings
from app.integrations.twilio_client import check_twilio_settings
from app.services.kizuna_notifications import KizunaNotifier, reprocess_pending_in_background

settings = get_settings()
configure_logging(settings.log_level)
# Falla al arrancar si la configuración de seguridad, de Twilio o del callback a KIZUNA es inválida (antes de crear
# la app).
check_security_settings(settings)
check_twilio_settings(settings)
check_kizuna_settings(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # M5: reenvía las notificaciones a KIZUNA pendientes, en segundo plano (no retrasa el arranque).
    async with reprocess_pending_in_background(KizunaNotifier(settings, get_session_factory())):
        yield


app = FastAPI(
    title="Yamaha Consent API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

# El último middleware agregado es el más externo: RequestIdMiddleware envuelve a CORS y registra también los
# preflight (OPTIONS).
add_cors(app, settings)
app.add_middleware(RequestIdMiddleware)

# Todas las respuestas de error en español y en JSON.
app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health.router)
app.include_router(kizuna.router)
app.include_router(public.router)
app.include_router(webhooks.router)
