from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.surface import include_routers
from app.core.backoffice_security import (
    BACKOFFICE_PREFIX,
    BackofficeError,
    backoffice_error_handler,
    check_backoffice_settings,
)
from app.core.config import get_settings
from app.core.http_errors import http_exception_handler, unhandled_exception_handler
from app.core.logging import configure_logging
from app.core.middleware import BackofficeHeadersMiddleware, RequestIdMiddleware
from app.core.security import add_cors, check_security_settings
from app.core.validation_errors import request_validation_exception_handler
from app.db.session import get_session_factory
from app.integrations.email import check_email_settings
from app.integrations.kizuna_client import check_kizuna_settings
from app.integrations.twilio_client import check_twilio_settings
from app.services.kizuna_notifications import KizunaNotifier, reprocess_pending_in_background

settings = get_settings()
configure_logging(settings.log_level)
# Falla al arrancar si la configuración de seguridad, de Twilio, del callback a KIZUNA, del BackOffice o de su correo
# es inválida
# (antes de crear la app).
check_security_settings(settings)
check_twilio_settings(settings)
check_kizuna_settings(settings)
check_backoffice_settings(settings)
check_email_settings(settings)


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
app.add_middleware(BackofficeHeadersMiddleware, prefix=BACKOFFICE_PREFIX)
app.add_middleware(RequestIdMiddleware)

# Todas las respuestas de error en español y en JSON.
app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
# Errores del BackOffice: {"detail", "code", "field"?} (BACKOFFICE_DEALERS.spec.md, sección 5.8).
app.add_exception_handler(BackofficeError, backoffice_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

# API_SURFACE: todos los endpoints (local y VPS), sin los de KIZUNA (servicio público) o solo los de KIZUNA (servicio
# privado con Cloud Run IAM).
include_routers(app, settings)
