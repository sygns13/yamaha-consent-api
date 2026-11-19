"""Qué endpoints expone cada servicio (API_SURFACE; INTEGRACION_KIZUNA_CLOUD_RUN_IAM.spec.md, sección 4.1).

- all: todos (local y VPS).
- public: sin los de KIZUNA (Cloud Run consent-api: landing, webhook de Twilio y BackOffice).
- kizuna: solo los de KIZUNA (Cloud Run consent-api-kizuna, privado con Cloud Run IAM).

/health está en los tres. Una ruta no montada responde 404.
"""

from fastapi import APIRouter, FastAPI

from app.api.v1 import backoffice, health, kizuna, public, webhooks
from app.core.config import Settings


def routers_for(settings: Settings) -> list[APIRouter]:
    routers = [health.router]
    if settings.api_surface in ("all", "kizuna"):
        routers.append(kizuna.router)
    if settings.api_surface in ("all", "public"):
        routers.extend([public.router, webhooks.router, backoffice.router])
    return routers


def include_routers(app: FastAPI, settings: Settings) -> None:
    for router in routers_for(settings):
        app.include_router(router)
