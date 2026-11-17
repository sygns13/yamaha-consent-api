"""API del BackOffice (BACKOFFICE_DEALERS.spec.md, sección 6): router agregador de /api/v1/backoffice/*.

Toda mutación (POST/PUT/PATCH/DELETE) exige la cabecera `X-Requested-With: XMLHttpRequest` (sección 5.6). Las
respuestas llevan `Cache-Control: no-store` y `X-Content-Type-Options: nosniff` (BackofficeHeadersMiddleware).

Cada sub-router declara el prefijo completo (`f"{BACKOFFICE_PREFIX}/auth"`) y el agregador no tiene prefijo: en
FastAPI 0.162 el prefijo de un router incluido dentro de otro no forma parte de `route.path`, y RequestIdMiddleware
registra `route.path` (quedaría `/auth/login` en lugar de `/api/v1/backoffice/auth/login`).
"""

from fastapi import APIRouter, Depends

from app.api.v1.backoffice import audit, auth, dealers, profile, ubigeo, users
from app.core.backoffice_security import require_csrf_header

router = APIRouter(dependencies=[Depends(require_csrf_header)])
router.include_router(auth.router)
router.include_router(profile.router)
router.include_router(users.router)
router.include_router(dealers.router)
router.include_router(ubigeo.router)
router.include_router(audit.router)
