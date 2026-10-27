import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.v1 import health
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware
from app.core.validation_errors import request_validation_exception_handler

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title="Yamaha Consent API",
    version="0.1.0",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

app.add_middleware(RequestIdMiddleware)
app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

app.include_router(health.router)

if not settings.api_key_enabled:
    logging.getLogger(__name__).warning("API_KEY_ENABLED=false: la validación de API Key está DESACTIVADA")
