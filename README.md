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
uv run pytest                    # todos los tests (usa la base consents_db_test, que se crea y migra sola)
uv run pytest -m integration     # solo los de punta a punta (tests/test_e2e_local.py)
```

Los tests de punta a punta recorren el flujo completo:
1. KIZUNA envía el cliente y se genera el enlace.
2. El cliente abre el enlace y envía sus respuestas.
3. El backend envía el callback al mock de KIZUNA.

Cubren aceptación, RN-01, RN-04 y RN-05. Los callbacks salen por HTTP real al mock de docker compose (`docker compose up -d`). Si el mock no
responde, se saltan; otra URL: `KIZUNA_MOCK_URL=http://host:puerto`.

## Postman

Colección en `postman/collection.json`: health, endpoints de KIZUNA, landing, webhook de Twilio y mock de KIZUNA.
Variables: `base_url`, `api_key`, `token`, `transaction_id`, `kizuna_mock_url`, `twilio_auth_token`, `message_sid`.

El token del enlace solo viaja en el WhatsApp y nunca se guarda ni se loguea. Para obtener uno en local, después de
`Registrar cliente`:

```bash
uv run python -m scripts.issue_local_link TRX-000123   # imprime token=... y la URL de la landing
```

El script reemplaza el token del enlace de esa transacción (el anterior deja de servir). Solo funciona con un
registro principal `PENDIENTE` y no corre con `APP_ENV=production`.

Para el webhook de Twilio, con `twilio_auth_token` igual a `TWILIO_AUTH_TOKEN` la colección calcula la firma
`X-Twilio-Signature`. Si se deja vacío, el webhook solo acepta la petición con `TWILIO_SIGNATURE_VALIDATION_ENABLED=false`.
