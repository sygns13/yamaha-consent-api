# syntax=docker/dockerfile:1
# Imagen del backend para Cloud Run (M7); también la usa docker compose (perfil api).
# No incluye .env ni secretos (ver .dockerignore): en Cloud Run llegan por --env-vars-file / --set-secrets.

# --- Etapa 1: dependencias con uv ---
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Solo las dependencias de producción, exactamente las de uv.lock (capa cacheada mientras no cambie el lock).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- Etapa 2: imagen final (sin uv ni caché de compilación) ---
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Usuario sin privilegios. El código queda de root: la aplicación no puede modificarlo.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER appuser

# Cloud Run define PORT (8080 por defecto). exec: uvicorn recibe el SIGTERM de Cloud Run y cierra ordenadamente.
# --forwarded-allow-ips: detrás del proxy de Cloud Run la app ve https en request.url (lo usa la validación de la firma
# de Twilio cuando TWILIO_STATUS_CALLBACK_URL no coincide). La IP de la evidencia se lee de X-Forwarded-For (M4).
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips '*'"]
