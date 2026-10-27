# yamaha-consent-api

Backend (FastAPI) de la plataforma de conformidades de tratamiento de datos personales (KIZUNA · WhatsApp · Landing).
La especificación completa está en `REQUERIMIENTOS_TECNICOS.md`.

## Requisitos locales

- [uv](https://docs.astral.sh/uv/) (instala Python 3.12 automáticamente)
- Docker Desktop con PostgreSQL 16 en el puerto **5433** (contenedor `yamaha-consent-db`)

## Levantar en local

```bash
uv sync                      # crea .venv e instala dependencias
cp .env.example .env         # PowerShell: Copy-Item .env.example .env
uv run uvicorn app.main:app --reload --port 8000
```

- Health check: http://localhost:8000/health → `{"status":"ok","database":"ok"}`
- Swagger: http://localhost:8000/docs

## Migraciones (Alembic)

```bash
uv run alembic current                                  # verifica la conexión
uv run alembic revision --autogenerate -m "mensaje"     # genera una migración desde los modelos
uv run alembic upgrade head                             # aplica migraciones
```

## Calidad

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```
