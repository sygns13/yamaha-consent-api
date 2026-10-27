# yamaha-consent-api

Backend (FastAPI) de la plataforma de conformidades de tratamiento de datos personales (KIZUNA · WhatsApp · Landing).
La especificación completa está en `REQUERIMIENTOS_TECNICOS.md`.

## Requisitos locales

- [uv](https://docs.astral.sh/uv/) (instala Python 3.12 automáticamente)
- Docker Desktop

## Levantar en local

```bash
uv sync                      # crea .venv e instala dependencias
cp .env.example .env         # PowerShell: Copy-Item .env.example .env
docker compose up -d         # PostgreSQL 16 (puerto 5433) + mock de KIZUNA (puerto 8081)
uv run alembic upgrade head  # crea el esquema (solo la primera vez o tras nuevas migraciones)
uv run uvicorn app.main:app --reload --port 8000
```

- Health check: http://localhost:8000/health → `{"status":"ok","database":"ok"}`
- Swagger: http://localhost:8000/docs
- Si ya tienes otro PostgreSQL en el puerto 5433 (por ejemplo, el contenedor `yamaha-consent-db`), detenlo antes o
  levanta solo el mock: `docker compose up -d kizuna-mock`.

### API también en contenedor

```bash
docker compose --profile api up -d --build
docker compose exec api alembic upgrade head
```

Dentro del compose, la API usa `postgres:5432` y `http://kizuna-mock:8081` (el compose sobrescribe `DATABASE_URL` y
`KIZUNA_CALLBACK_URL` del `.env`).

### Mock de KIZUNA

- `POST /kizuna/consent-result`: valida el payload y responde `200 {"received": true}`.
- `GET /kizuna/received`: payloads recibidos y total de peticiones. `DELETE /kizuna/received`: reinicia.
- Simular fallos: `MOCK_FAIL_TIMES=3 docker compose up -d kizuna-mock` (500 las 3 primeras veces) o
  `MOCK_RESPONSE_DELAY_SECONDS=15` (timeout).

## Configuración

Todas las variables están en `.env.example` (sección 9 de la spec). `DATABASE_URL` es la URL completa, incluida la
contraseña. Los logs salen en JSON por stdout con `request_id` (cabecera `X-Request-ID`); los datos personales se
enmascaran con `app/utils/masking.py`.

## Migraciones (Alembic)

```bash
uv run alembic current                                  # verifica la conexión
uv run alembic revision --autogenerate -m "mensaje"     # genera una migración desde los modelos (revisarla a mano)
uv run alembic upgrade head                             # aplica migraciones
uv run alembic downgrade base                           # revierte todo el esquema
```

## Calidad

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```
