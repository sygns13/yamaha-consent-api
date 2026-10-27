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

### Probar la imagen de Cloud Run en local

La misma imagen que se despliega, sin `.env` (como en Cloud Run: solo variables de entorno), contra el PostgreSQL de
docker compose:

```bash
docker build -t consent-api:local .
docker run --rm --network yamaha-consent-api_default \
  -e DATABASE_URL=postgresql+asyncpg://postgres:1234@postgres:5432/consents_db consent-api:local alembic current
docker run --rm --network yamaha-consent-api_default -p 8090:8080 \
  -e APP_ENV=production -e API_KEY_ENABLED=true -e API_KEY=clave-local \
  -e DATABASE_URL=postgresql+asyncpg://postgres:1234@postgres:5432/consents_db \
  -e KIZUNA_CALLBACK_URL=http://kizuna-mock:8081/kizuna/consent-result \
  consent-api:local
curl http://localhost:8090/health    # {"status":"ok","database":"ok"}
```

La imagen se construye en dos etapas: la final no incluye uv. Corre con el usuario `appuser` (uid 10001), que no es root y
no puede modificar el código. Uvicorn escucha en `$PORT` (8080 por defecto) y confía en los headers del proxy de Cloud
Run (`--forwarded-allow-ips '*'`). La imagen no incluye `.env`, `tests/`, `mocks/`, `scripts/`, `postman/` ni `deploy/`
(ver `.dockerignore`).

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

## Despliegue manual en Google Cloud (M7)

No hay CI/CD: el despliegue es manual y solo a Producción. Los comandos son para Git Bash o Cloud Shell (en PowerShell,
definir las variables con `$env:NOMBRE = "valor"` y continuar las líneas con `` ` `` en vez de `\`).

### 0. Requisitos y variables

- `gcloud` (Google Cloud CLI), Docker y el [Cloud SQL Auth Proxy v2](https://cloud.google.com/sql/docs/postgres/sql-proxy).
- Una cuenta con acceso al proyecto de Yamaha: `gcloud auth login` y `gcloud auth application-default login` (esta
  última la usa el proxy).

```bash
export PROJECT_ID=<ID_PROYECTO>              # lo provee Yamaha
export REGION=<REGION>                       # PENDIENTE-09 (p. ej. us-east1)
export INSTANCE=consents-db                  # instancia de Cloud SQL
export INSTANCE_CONNECTION=$PROJECT_ID:$REGION:$INSTANCE
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/consent-api/consent-api
export TAG=$(git rev-parse --short HEAD)     # un tag por versión (no usar solo "latest")
gcloud config set project $PROJECT_ID
```

### 1. Recursos (solo la primera vez)

```bash
# APIs
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com logging.googleapis.com

# Artifact Registry: repositorio Docker "consent-api"
gcloud artifacts repositories create consent-api --repository-format=docker --location=$REGION

# Cloud SQL: PostgreSQL 16, tier pequeño (a validar con Yamaha), backups automáticos
gcloud sql instances create $INSTANCE --database-version=POSTGRES_16 --edition=ENTERPRISE \
  --tier=db-g1-small --region=$REGION --backup-start-time=08:00
gcloud sql databases create consents_db --instance=$INSTANCE
# Dueño del esquema: lo usan las migraciones (paso 3)
gcloud sql users create consent_owner --instance=$INSTANCE --password=<CLAVE_OWNER>

# Service Account del servicio
gcloud iam service-accounts create consent-api-sa --display-name="consent-api (Cloud Run)"
SA=consent-api-sa@$PROJECT_ID.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/cloudsql.client
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
```

**Usuario de la aplicación con privilegios mínimos.** Los usuarios creados con `gcloud sql users create` reciben
`cloudsqlsuperuser`, así que el de la aplicación se crea por SQL. Hay que hacerlo **después** de la primera migración
(paso 3), con el proxy levantado: `psql "host=127.0.0.1 port=5434 dbname=consents_db user=consent_owner"`.

```sql
CREATE ROLE consent_app LOGIN PASSWORD '<CLAVE_APP>';
GRANT CONNECT ON DATABASE consents_db TO consent_app;
GRANT USAGE ON SCHEMA public TO consent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO consent_app;
-- Tablas que creen las migraciones futuras (las crea consent_owner):
ALTER DEFAULT PRIVILEGES FOR ROLE consent_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO consent_app;
```

**Secretos** en Secret Manager. Se cargan desde la entrada estándar para que no queden en el historial de la shell ni
en un archivo:

```bash
# DATABASE_URL: URL completa con el socket de Cloud SQL (caracteres especiales de la clave codificados en URL: @ → %40)
#   postgresql+asyncpg://consent_app:<CLAVE_APP>@/consents_db?host=/cloudsql/<PROJECT_ID>:<REGION>:<INSTANCE>
gcloud secrets create DATABASE_URL --replication-policy=automatic --data-file=-     # pegar la URL, Enter y Ctrl+D
gcloud secrets create API_KEY --replication-policy=automatic --data-file=-          # la clave que usará KIZUNA
gcloud secrets create TWILIO_AUTH_TOKEN --replication-policy=automatic --data-file=-
# Solo cuando se conozca la autenticación de KIZUNA (PENDIENTE-13):
gcloud secrets create KIZUNA_CALLBACK_AUTH_VALUE --replication-policy=automatic --data-file=-
```

Para cambiar un secreto, se agrega una versión con `gcloud secrets versions add <NOMBRE> --data-file=-` y se vuelve a
desplegar (paso 4).

### 2. Construir y subir la imagen a Artifact Registry

```bash
gcloud auth configure-docker $REGION-docker.pkg.dev
docker build --platform linux/amd64 -t $IMAGE:$TAG .
docker push $IMAGE:$TAG
```

Alternativa sin Docker local: `gcloud builds submit --tag $IMAGE:$TAG .` (requiere la API de Cloud Build).

### 3. Migraciones con el Cloud SQL Auth Proxy

Siempre **antes** del deploy: el código nuevo espera el esquema nuevo. Mientras se despliega, la revisión anterior
sigue atendiendo, así que las migraciones deben ser compatibles con ella (agregar antes que borrar).

```bash
# Terminal 1: proxy en el puerto 5434 (el 5433 lo usa el PostgreSQL local)
cloud-sql-proxy --port 5434 $INSTANCE_CONNECTION

# Terminal 2: desde este repositorio, en el mismo commit que la imagen ($TAG), con el usuario dueño del esquema
DATABASE_URL="postgresql+asyncpg://consent_owner:<CLAVE_OWNER>@127.0.0.1:5434/consents_db" uv run alembic upgrade head
DATABASE_URL="postgresql+asyncpg://consent_owner:<CLAVE_OWNER>@127.0.0.1:5434/consents_db" uv run alembic current
```

`DATABASE_URL` en la línea de comandos tiene prioridad sobre el `.env` local. Al terminar, cerrar el proxy (Ctrl+C).
Si la clave tiene caracteres especiales, va codificada en URL.

### 4. Desplegar en Cloud Run

1. Completar los valores `<...>` de `deploy/cloudrun.env.yaml`. Son las variables no secretas; el archivo reemplaza
   todas las del servicio.
2. Desplegar:

```bash
gcloud run deploy consent-api \
  --image=$IMAGE:$TAG \
  --region=$REGION \
  --service-account=consent-api-sa@$PROJECT_ID.iam.gserviceaccount.com \
  --min-instances=1 \
  --no-cpu-throttling \
  --add-cloudsql-instances=$INSTANCE_CONNECTION \
  --allow-unauthenticated \
  --env-vars-file=deploy/cloudrun.env.yaml \
  --set-secrets=DATABASE_URL=DATABASE_URL:latest,API_KEY=API_KEY:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest
```

- `--min-instances=1` y `--no-cpu-throttling` (CPU siempre asignada) son obligatorios. Los callbacks a KIZUNA y sus
  reintentos corren después de responder (M5), y sin CPU asignada se congelarían.
- `--allow-unauthenticated`: el servicio es público porque lo llaman KIZUNA y Twilio. Los endpoints de KIZUNA se
  protegen con la API Key y el webhook con la firma de Twilio.
- `--set-secrets` reemplaza todos los secretos del servicio. Cuando exista `KIZUNA_CALLBACK_AUTH_VALUE`, agregar
  `,KIZUNA_CALLBACK_AUTH_VALUE=KIZUNA_CALLBACK_AUTH_VALUE:latest`. Para fijar una versión concreta, usar `:<número>`
  en vez de `:latest`.
- Si falta un dato obligatorio la app no arranca y el deploy falla, y la revisión anterior sigue activa. Puede faltar
  la `API_KEY`, la configuración de Twilio con `TWILIO_SEND_ENABLED=true` o la de KIZUNA con
  `KIZUNA_CALLBACK_ENABLED=true`. El motivo aparece en los logs con severidad `CRITICAL`.

### 5. Verificar

```bash
URL=$(gcloud run services describe consent-api --region=$REGION --format='value(status.url)')
curl $URL/health                                   # {"status":"ok","database":"ok"}
gcloud run services logs read consent-api --region=$REGION --limit=50
```

En los logs del arranque deben aparecer solo los `WARNING` esperados, por ejemplo `KIZUNA_CALLBACK_ENABLED=false`, y
ningún `CRITICAL`.

En el **primer** deploy, la URL de Cloud Run recién se conoce aquí:
1. Poner `TWILIO_STATUS_CALLBACK_URL=$URL/api/v1/webhooks/twilio/status` en `deploy/cloudrun.env.yaml`.
2. Repetir el paso 4.
3. Configurar esa misma URL como `status_callback` en Twilio.

### 6. Volver a la versión anterior

```bash
gcloud run revisions list --service=consent-api --region=$REGION
gcloud run services update-traffic consent-api --region=$REGION --to-revisions=<REVISION_ANTERIOR>=100
```

Las migraciones no se revierten solas. Hacer `alembic downgrade` solo si la versión anterior no funciona con el
esquema nuevo, y revisando antes qué borra.

### Pendientes antes de habilitar todo en Producción

- `TWILIO_SEND_ENABLED=true`: cuando la plantilla esté aprobada por Meta y se conozca `TWILIO_CONTENT_SID`
  (PENDIENTE-07 y PENDIENTE-15).
- `KIZUNA_CALLBACK_ENABLED=false` hasta tener el endpoint real de KIZUNA y su contrato (PENDIENTE-13). Al encenderlo,
  el reproceso del arranque envía las notificaciones acumuladas.
- `DOCS_ENABLED` (PENDIENTE-12), `PRIVACY_POLICY_URL` (PENDIENTE-14) y el dominio de la landing en
  `CORS_ALLOWED_ORIGINS` / `LANDING_BASE_URL` (PENDIENTE-15).
