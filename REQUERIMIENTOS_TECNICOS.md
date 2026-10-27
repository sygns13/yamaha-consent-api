# Hoja de Requerimientos Técnicos — Gestión Digital de Conformidades (KIZUNA · WhatsApp · Landing)

| Campo | Valor |
|---|---|
| Cliente | Yamaha – Perú |
| Proveedor / Responsable técnico | Cristian Fernando Chávez Torres |
| Versión del documento | 1.3 — 27/10/2026 (incorpora las decisiones de la sección 11) |
| Nube | Google Cloud (proyecto provisto por Yamaha). **Base de datos, backend y landing se despliegan en Google Cloud** |
| Stack | Python + FastAPI · PostgreSQL (Cloud SQL) · Astro · Twilio WhatsApp |
| Ambiente | Solo Producción (desarrollo y pruebas en local) |

---

## 0. Cómo usar este documento (instrucciones para agentes de IA)

Este documento es la **fuente de verdad** para construir la solución. Si trabajas con un asistente de código (Claude Code, Gemini, etc.):

1. Construye **un módulo a la vez** siguiendo el orden de la sección 3 (de la capa más baja a la más alta). No avances al siguiente módulo sin cumplir los **criterios de aceptación** del actual.
2. **No inventes reglas de negocio.** Todo lo marcado como `⚠️ PENDIENTE-n` está abierto: implementa solo lo indicado como "mientras tanto" y deja un `TODO(PENDIENTE-n)` en el código.
3. Todo valor marcado como **configurable** se lee de variables de entorno (sección 9). Nunca lo escribas fijo en el código.
4. Lo marcado como **(propuesta)** es una recomendación técnica que el responsable aprobó como punto de partida; puede cambiar.
5. Convención: **código, nombres de tablas y campos en inglés**; **textos visibles al usuario en español**.
6. **Nunca** escribas datos personales (documento, nombre, teléfono, IP) en logs sin enmascarar.

---

## 1. Contexto y alcance

Plataforma para gestionar digitalmente las conformidades de tratamiento de datos personales de los clientes finales de Yamaha:

1. **KIZUNA** (operado al 100% por Yamaha, fuera de alcance) envía los datos del cliente al **Backend**.
2. El Backend registra el cliente en la **Base de Datos** y envía un **WhatsApp** (Twilio) con un enlace único.
3. El cliente abre la **Landing**, marca o desmarca 3 autorizaciones y envía.
4. El Backend registra las respuestas y **notifica a KIZUNA** (con reintentos). KIZUNA también puede **consultar** los estados a demanda.

### 1.1 Dentro del alcance

- Base de datos (Cloud SQL – PostgreSQL).
- Backend API (FastAPI): recepción desde KIZUNA, consulta, notificación a KIZUNA, API pública para la landing y webhook de Twilio.
- Integración con Twilio WhatsApp (número nuevo y exclusivo, plantilla, envío y registro de estados y errores).
- Landing de conformidad, página de agradecimiento y página de enlace inválido o vencido (Astro).
- Aprovisionamiento en Google Cloud y **despliegue manual** en Producción.

### 1.2 Fuera del alcance

- Cualquier configuración o desarrollo dentro de KIZUNA, incluida la RN-02 (validación de conformidad vigente) y el envío a D365.
- Reintentos o reenvíos del WhatsApp cuando el cliente no abre el enlace.
- Paneles de administración, reportes o formularios adicionales.
- **CI/CD automatizado** (solo despliegue manual).
- Ambientes de QA o Desarrollo en la nube.

### 1.3 Reglas de negocio aplicables (ajustadas)

| ID | Regla | Dónde se implementa |
|---|---|---|
| RN-01 | **Prevalece sobre el mockup.** El cliente **puede enviar sin marcar ningún check**. Si el check 1 (Tratamiento de Datos Personales) queda desmarcado, el estado final es **NO ACEPTADO**: se aplica **borrado lógico** (`is_deleted = 1`), se **anula el nombre del cliente** (el teléfono se conserva por trazabilidad) y no se conservan las respuestas de marketing. Se notifica a KIZUNA. | Backend (M4, M5) · Landing (M6) |
| RN-02 | Conformidad vigente para el mismo Dealer. | **KIZUNA — no aplica a este desarrollo** |
| RN-03 | La conformidad está asociada al Dealer. Un mismo documento con **otro Dealer** genera un registro nuevo, un WhatsApp nuevo y un enlace nuevo. | Backend (M2) |
| RN-04 | Si llega información de un cliente con un **registro activo** (`is_deleted = 0`) para el mismo Dealer, se **actualizan los datos del cliente** (nombre, teléfono, información adicional) en ese registro, **no** se reenvía el WhatsApp y sus conformidades se mantienen. Si llega con un **nuevo ID de transacción**, ese ID **se registra como copia vinculada** del registro activo (sin enlace ni WhatsApp) con las respuestas que el cliente ya dio; si el cliente aún no responde, la copia queda `PENDIENTE` y **recibe la respuesta** cuando el cliente conteste en el registro original. KIZUNA recibe **una respuesta final por cada ID de transacción**. | Backend (M2, M4, M5) |
| RN-05 | **Nueva compra tras un rechazo:** si el único registro previo del documento + Dealer está borrado lógicamente (NO ACEPTADO), se crea un **registro nuevo** con el nuevo ID de transacción y se envía un **nuevo WhatsApp** para que el cliente pueda aceptar. Por eso el documento + Dealer **no es único**: la **PK es el ID de transacción**. | Backend (M2) |

### 1.4 Diagramas y mockups de referencia

- Flujo original (referencial; el carril KIZUNA cambió según la sección 1): `./assets/flujo-proceso-original.png`
- Mockup de la landing: `./assets/mockup-landing.png`
- Mockup del WhatsApp: `./assets/mockup-whatsapp.png`

---

## 2. Arquitectura

```
                     ┌────────────────────────── Google Cloud (proyecto Yamaha) ───────────────────────────┐
                     │                                                                                     │
 KIZUNA ──POST /api/v1/consents──►┌──────────────────────┐      ┌──────────────────────────┐              │
 (Yamaha) ◄──callback (3 reint.)──│ Backend FastAPI      │◄────►│ Cloud SQL (PostgreSQL)   │              │
 KIZUNA ──GET /api/v1/consents ──►│ Cloud Run            │      └──────────────────────────┘              │
                     │            │                      │──secrets──► Secret Manager                      │
                     │            └───┬──────────▲───────┘                                                 │
                     │                │ envío    │ status callback                                         │
                     │                ▼          │                                                         │
                     │            Twilio WhatsApp API ───► Cliente (WhatsApp, Android/iOS)                 │
                     │                                         │ abre enlace (?t=token)                    │
                     │            ┌──────────────────────┐     ▼                                           │
                     │            │ Landing Astro (static)│◄── Navegador ──► /api/v1/public/... (Backend)  │
                     │            │ Firebase Hosting      │                                                │
                     │            └──────────────────────┘                                                 │
                     └─────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Tecnologías por componente

| Capa | Tecnología | Notas |
|---|---|---|
| Base de datos | **PostgreSQL 16** gestionado en **Cloud SQL** | Región: ⚠️ PENDIENTE-09 (la define Yamaha) |
| Migraciones | **Alembic** | Se ejecutan manualmente vía Cloud SQL Auth Proxy |
| Backend | **Python 3.12+**, **FastAPI**, **Uvicorn** | Async de punta a punta |
| ORM / acceso a datos | **SQLAlchemy 2.x (async)** + **asyncpg** | |
| Validación / config | **Pydantic v2** + **pydantic-settings** | Toda la configuración por variables de entorno |
| HTTP saliente (KIZUNA) | **httpx** (async) | Reintentos con backoff configurable |
| Twilio | **twilio** (SDK oficial de Python) + **Content API** (plantillas) | Validación de firma con `RequestValidator` |
| Gestión de dependencias | **uv** (propuesta) | `pyproject.toml` |
| Calidad | **ruff** (lint + format), **pytest**, **pytest-asyncio** | |
| Contenedor | Docker (`python:3.12-slim`) | |
| Cómputo backend | **Cloud Run** | `min-instances=1`, CPU siempre asignada (ver M7) |
| Secretos | **Secret Manager** | Montados en Cloud Run como variables de entorno |
| Imágenes | **Artifact Registry** | |
| Frontend | **Astro** (salida estática, `output: 'static'`), **TypeScript** | Sin frameworks de UI; JS mínimo |
| Estilos | **Tailwind CSS** (propuesta) o CSS plano | Colores corporativos de Yamaha (azul y blanco) según el mockup |
| Hosting landing | **Firebase Hosting** (en el mismo proyecto de Google Cloud) | Dominio propio con DNS en Hostinger y SSL automático |
| Logs | **Cloud Logging** (logs JSON a stdout) | Enmascarar datos personales |
| Desarrollo local | **docker compose** (PostgreSQL) + **ngrok** (webhook Twilio) + Postman | |

### 2.2 Repositorios (los crea el responsable)

| Repositorio (nombre propuesto) | Contenido |
|---|---|
| `yamaha-consent-api` | Backend FastAPI, migraciones Alembic, Dockerfile, docker compose local |
| `yamaha-consent-landing` | Proyecto Astro (landing, página de agradecimiento y página de enlace inválido) |

---

## 3. Orden de construcción (módulos)

| # | Módulo | Capa | Depende de |
|---|---|---|---|
| M0 | Base del proyecto y entorno local | Transversal | — |
| M1 | Base de datos: modelo y migraciones | Datos | M0 |
| M2 | Backend core: configuración, seguridad, normalización, API para KIZUNA (recepción y consulta) | Backend | M1 |
| M3 | Integración con Twilio WhatsApp (envío y webhook de estados) | Backend / Integración | M2 |
| M4 | API pública para la landing (validar enlace y registrar respuestas, RN-01) | Backend | M2 |
| M5 | Notificación a KIZUNA (outbox y reintentos) | Backend / Integración | M4 |
| M6 | Frontend Astro (landing, agradecimiento, enlace inválido) | Frontend | M4 |
| M7 | Infraestructura en Google Cloud y despliegue manual | Infraestructura | M1–M6 |
| M8 | Pruebas de punta a punta en Producción y entrega | Cierre | M7 |

---

## M0 — Base del proyecto y entorno local

**Requerimientos**

- Estructura del backend:
  ```
  yamaha-consent-api/
  ├─ app/
  │  ├─ main.py                # creación de la app, routers, middlewares
  │  ├─ core/                  # config (pydantic-settings), seguridad, logging
  │  ├─ db/                    # engine, session, base
  │  ├─ models/                # modelos SQLAlchemy
  │  ├─ schemas/               # modelos Pydantic (request/response)
  │  ├─ services/              # lógica de negocio (consents, links, notifications)
  │  ├─ integrations/          # twilio_client.py, kizuna_client.py
  │  ├─ api/v1/                # routers: kizuna.py, public.py, webhooks.py, health.py
  │  └─ utils/                 # normalización, enmascarado, tokens
  ├─ alembic/
  ├─ tests/
  ├─ mocks/
  │  └─ kizuna_mock/           # servicio simulado de KIZUNA (ver M5)
  ├─ Dockerfile
  ├─ docker-compose.yml        # postgres:16 + kizuna-mock en local
  ├─ .env.example
  └─ pyproject.toml
  ```
- `.env.example` con **todas** las variables de la sección 9.
- Logging estructurado en JSON con `request_id` por petición.
- Utilidad de enmascarado: documento `****5678`, teléfono `*****4321`.
- OpenAPI (Swagger) disponible en `/docs`. ⚠️ PENDIENTE-12: ¿debe quedar expuesto en Producción? Mientras tanto se controla con `DOCS_ENABLED`.
- `GET /health` verifica también la conexión a la base de datos: `200 {"status":"ok","database":"ok"}`; si la base no responde, `503 {"status":"error","database":"unreachable"}`.
- `docker compose up` levanta PostgreSQL y el mock de KIZUNA; la API se ejecuta en el host con `uv run uvicorn app.main:app --reload --port 8000`. Con `docker compose --profile api up` también se levanta la API en un contenedor (mismo `Dockerfile` que Cloud Run).

**Criterios de aceptación**

- `docker compose up` levanta PostgreSQL y el mock de KIZUNA, y la API responde `GET /health` → `200 {"status":"ok","database":"ok"}`.
- `ruff check` y `pytest` pasan sin errores.

---

## M1 — Base de datos (Cloud SQL – PostgreSQL)

### Requerimientos

- Fechas en `timestamptz` y almacenadas en **UTC**. En las respuestas de la API se devuelven en ISO 8601.
- La PK de `consents` es el **`transaction_id`** que envía KIZUNA. Las demás tablas usan PK `UUID` generada en la aplicación (`uuid4`) y se relacionan con `consents` por `transaction_id`.
- Todas las tablas llevan `created_at` y `updated_at`.
- **Borrado lógico:** columna `is_deleted` de tipo `SMALLINT NOT NULL DEFAULT 0` con `CHECK (is_deleted IN (0,1))`. Es el equivalente de `TINYINT` en PostgreSQL: `0` = vigente, `1` = borrado.

### Enums

| Enum | Valores |
|---|---|
| `document_type` | `DNI`, `RUC`, `CE` (Carné de extranjería), `PASAPORTE`, `OTROS` |
| `consent_status` | `PENDIENTE`, `ACEPTADO`, `NO_ACEPTADO` |
| `whatsapp_status` | `queued`, `sent`, `delivered`, `read`, `failed`, `undelivered` (los valores de Twilio) |
| `notification_status` | `PENDIENTE`, `ENVIADO`, `FALLIDO` |

### Tabla `consents` (registro central, uno por transacción)

| Campo | Tipo | Nulo | Descripción |
|---|---|---|---|
| `transaction_id` | varchar(100) **PK** | no | ID único de la transacción enviado por KIZUNA |
| `document_type` | document_type | no | Se conserva siempre |
| `document_number` | varchar(20) | no | Normalizado (sección M2). Se conserva siempre |
| `dealer` | varchar(100) | no | Se conserva siempre. ⚠️ PENDIENTE-05 (¿código, nombre o ambos?) |
| `full_name` | varchar(250) | **sí** | Normalizado. **Se anula en el borrado lógico (RN-01)** |
| `phone` | char(9) | no | 9 dígitos, sin prefijo de país. **Se conserva siempre, por trazabilidad** (también tras el rechazo) |
| `additional_info` | jsonb | sí | Campo libre para futuras integraciones (hoy sin campos definidos) |
| `status` | consent_status | no | Por defecto `PENDIENTE` |
| `consent_personal_data` | boolean | sí | Check 1 |
| `consent_marketing_yamaha` | boolean | sí | Check 2. Se anula en RN-01 (no se conservan las respuestas de marketing) |
| `consent_marketing_dealer` | boolean | sí | Check 3. Se anula en RN-01 |
| `responded_at` | timestamptz | sí | Fecha y hora de aceptación o rechazo |
| `is_deleted` | smallint | no | `0` por defecto; `1` = borrado lógico. `CHECK (is_deleted IN (0,1))` |
| `deleted_at` | timestamptz | sí | |
| `source_transaction_id` | varchar(100) FK → consents | sí | Solo en **copias vinculadas** (RN-04 con un ID nuevo): registro principal del que copia las respuestas. Nulo en los registros principales. `CHECK (source_transaction_id <> transaction_id)` |
| `created_at` / `updated_at` | timestamptz | no | |

- **Sin restricción única** sobre documento + Dealer: un mismo cliente puede tener varios registros con el mismo Dealer (RN-05).
- **Índices:** `(document_type, document_number, dealer, is_deleted)`, `status`, `created_at`, `source_transaction_id`.
- **Dos clases de registro:**
  - **Principal** (`source_transaction_id` nulo): tiene su enlace (`consent_links`) y su WhatsApp; el cliente responde en él.
  - **Copia vinculada** (`source_transaction_id` con el ID del principal): se crea en RN-04 con un ID de transacción nuevo. No tiene enlace ni WhatsApp; copia el estado y las respuestas del principal.
- **Registro activo** de un documento + Dealer: el **principal** más reciente con `is_deleted = 0`. Se garantiza que haya **como máximo uno** con el índice único parcial `uq_consents_active_document_dealer` `WHERE is_deleted = 0 AND source_transaction_id IS NULL`. Las copias vinculadas activas no cuentan.

### Tabla `consent_requests` (historial de solicitudes recibidas desde KIZUNA)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | uuid PK | |
| `transaction_id` | varchar(100) | ID de transacción recibido en la solicitud |
| `consent_transaction_id` | varchar(100) FK → consents | Registro afectado |
| `action` | varchar(20) | `CREATED` o `UPDATED` (RN-04) |
| `received_at` | timestamptz | |

> No guarda el payload completo para no duplicar datos personales.

### Tabla `consent_links` (enlaces únicos de la landing)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | uuid PK | |
| `transaction_id` | varchar(100) FK → consents | |
| `token_hash` | char(64) UNIQUE | SHA-256 del token. **El token en claro nunca se guarda** |
| `expires_at` | timestamptz | `created_at + LINK_TTL_HOURS` (por defecto 168 h = 1 semana) |
| `used_at` | timestamptz null | Se marca al enviar las respuestas (un solo uso) |
| `created_at` | timestamptz | |

### Tabla `consent_evidence` (evidencia del consentimiento)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | uuid PK | |
| `transaction_id` | varchar(100) FK → consents | |
| `link_id` | uuid FK → consent_links | |
| `ip_address` | inet | Primera IP de `X-Forwarded-For` |
| `user_agent` | text | |
| `policy_version` | varchar(20) | `PRIVACY_POLICY_VERSION` |
| `policy_url` | text | `PRIVACY_POLICY_URL` |
| `answers` | jsonb | `{"personal_data":bool,"marketing_yamaha":bool,"marketing_dealer":bool}`, exactamente lo que envió el cliente (también en un rechazo) |
| `submitted_at` | timestamptz | |

> **Decisión:** el borrado lógico **no** afecta esta tabla ni `whatsapp_messages`; solo se anula el nombre del cliente en `consents`.

### Tabla `whatsapp_messages`

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | uuid PK | |
| `transaction_id` | varchar(100) FK → consents | |
| `link_id` | uuid FK → consent_links | |
| `twilio_message_sid` | varchar(64) UNIQUE null | Nulo si falló antes de llegar a Twilio |
| `to_phone` | varchar(30) | `whatsapp:+51XXXXXXXXX` (21 caracteres; 30 admite un número E.164 completo, ver PENDIENTE-10) |
| `content_sid` | varchar(64) | Plantilla usada |
| `status` | whatsapp_status | Último estado conocido |
| `error_code` | varchar(20) null | Código de error de Twilio |
| `error_message` | text null | |
| `created_at` / `updated_at` | timestamptz | |

### Tabla `whatsapp_message_events` (registro de cada callback de Twilio)

`id`, `message_id` FK, `status`, `error_code`, `raw_payload` jsonb, `received_at`.

- **UNIQUE (`message_id`, `status`)**: un evento por mensaje y estado; hace idempotente el webhook (migración `a43dda5e709f`).
- `raw_payload` guarda el formulario completo que envía Twilio (incluye el teléfono del cliente): no se loguea.

### Tabla `kizuna_notifications` (outbox de notificaciones a KIZUNA)

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | uuid PK | |
| `transaction_id` | varchar(100) FK → consents | |
| `payload` | jsonb | Cuerpo enviado a KIZUNA (o al mock) |
| `target_url` | text | URL a la que se envió |
| `status` | notification_status | |
| `attempts` | int | Intentos realizados |
| `last_http_status` | int null | |
| `last_error` | text null | |
| `last_attempt_at` / `sent_at` | timestamptz null | |
| `claimed_at` | timestamptz null | Reclamo del envío (M5): se marca al reclamar la notificación (`SELECT ... FOR UPDATE SKIP LOCKED`) y se renueva en cada intento. Nulo = sin reclamar (migración `94fef8d63ecf`) |

### Criterios de aceptación M1

- `alembic upgrade head` crea todo el esquema en una base vacía; `alembic downgrade base` lo revierte.
- `consents.transaction_id` es PK: insertar dos veces el mismo ID de transacción falla.
- Se pueden insertar dos registros con el mismo documento + Dealer si el anterior tiene `is_deleted = 1`.
- Se pueden insertar copias vinculadas activas junto al registro principal activo; dos principales activos del mismo documento + Dealer fallan.
- `is_deleted` rechaza valores distintos de 0 y 1.

---

## M2 — Backend core y API para KIZUNA

### 2.1 Configuración y seguridad

- La configuración se carga con `pydantic-settings` (sección 9).
- **API Key** para los endpoints de KIZUNA:
  - Header: `X-API-Key` (nombre configurable con `API_KEY_HEADER`; el nombre del header no distingue mayúsculas, como en todo HTTP).
  - `API_KEY_ENABLED=true|false`. Con `false` **no se valida** (pruebas locales o con Postman). Al arrancar, si está en `false` se emite un log `WARNING`.
  - **Con `API_KEY_ENABLED=true` y `API_KEY` vacía (o solo espacios), la aplicación no arranca:** lanza una excepción (`InsecureConfigurationError`) y registra un log `CRITICAL`. En Cloud Run la revisión no queda activa y el despliegue falla. El chequeo se hace al arrancar la API, no al cargar la configuración, para no bloquear las migraciones de Alembic.
  - Comparación **exacta** y en tiempo constante (`secrets.compare_digest`, sobre bytes UTF-8): no se recortan espacios ni se ignoran mayúsculas en el valor.
  - Sin clave → `401 {"detail": "Falta la API Key"}`. Con clave inválida → `401 {"detail": "API Key inválida"}`.
  - Los rechazos se registran en el log con el motivo y la ruta, **nunca** con la clave recibida ni la IP.
  - La API Key se documenta en OpenAPI (esquema `ApiKey`, botón *Authorize* de Swagger).
- Los endpoints públicos (`/api/v1/public/*`) **no** usan API Key: los protege el token del enlace.
- CORS: solo los orígenes de `CORS_ALLOWED_ORIGINS` (dominio de la landing). Se permiten únicamente `GET` y `POST` con el header `Content-Type`, sin credenciales (cookies), y se expone `X-Request-ID`. El header de la API Key **no** se permite desde el navegador: KIZUNA llama de servidor a servidor y no necesita CORS.

### 2.2 Normalización y validación de entrada

| Campo | Normalización | Validación |
|---|---|---|
| `document_type` | Mayúsculas | Debe estar en el enum |
| `document_number` | Trim, quitar espacios y guiones, mayúsculas | DNI: 8 dígitos · RUC: 11 dígitos · CE: alfanumérico, máximo 12 · PASAPORTE: alfanumérico, máximo 12 · OTROS: alfanumérico, máximo 20 **(propuesta)**. *Alfanumérico* = letras A–Z, **Ñ** y dígitos 0–9 (sin tildes) |
| `full_name` | Trim, colapsar espacios múltiples, **MAYÚSCULAS conservando tildes y Ñ** (`str.upper()`) | No vacío, máximo 250 |
| `phone` | Quitar todo lo que no sea dígito; si quedan 11 dígitos y empieza con `51`, quitar `51` | Exactamente **9 dígitos** (formato `XXXXXXXXX`) |
| `dealer` | Trim | No vacío (⚠️ PENDIENTE-05) |
| `additional_info` | — | Objeto JSON opcional |

> Al enviar por Twilio, el teléfono se arma como `whatsapp:+51` + `phone`. ⚠️ PENDIENTE-10: los números extranjeros no son compatibles con la regla de 9 dígitos.

Un error de validación responde `422` con el detalle del campo: `{"detail": [{"type": "...", "loc": ["body", "phone"], "msg": "..."}]}`. Los mensajes (`msg`) están **en español**, incluidos los que genera Pydantic. La respuesta **no** devuelve el valor recibido (`input`), porque puede ser un dato personal.

**Otros errores HTTP**, también en español y en JSON (`{"detail": "..."}`). Se traducen los textos que generan FastAPI y Starlette; los mensajes propios en español pasan sin cambios:

| Caso | Respuesta |
|---|---|
| Ruta inexistente | `404 {"detail": "Recurso no encontrado"}` |
| Método no permitido | `405 {"detail": "Método no permitido"}` (se conserva el header `Allow`) |
| Cuerpo ilegible (p. ej. no está en UTF-8) | `400 {"detail": "No se pudo leer el cuerpo de la petición: debe ser JSON en UTF-8"}`. FastAPI lee el cuerpo antes de validar la API Key, así que este error puede llegar antes que un `401` |
| Error no controlado | `500 {"detail": "Error interno del servidor"}`, sin detalles internos (antes era texto plano en inglés) |
| Otro código sin detalle propio | Mensaje genérico en español según el código (por ejemplo `503 "Servicio no disponible"`) |

- El engine de SQLAlchemy usa `hide_parameters=True`: los errores de base de datos (por ejemplo un `IntegrityError` en el traceback de un `500`) no incluyen los parámetros del SQL, que traen datos personales.

### 2.3 `POST /api/v1/consents` — recepción desde KIZUNA (requiere API Key)

**Request**
```json
{
  "transaction_id": "TRX-000123",
  "document_type": "DNI",
  "document_number": "12345678",
  "full_name": "juan  pérez   gómez",
  "phone": "+51 987 654 321",
  "dealer": "DEALER-001",
  "additional_info": {}
}
```

**Lógica** (en una transacción, bloqueando por documento + Dealer para evitar duplicados concurrentes)

- **Bloqueo:** *advisory locks* de PostgreSQL (`pg_advisory_xact_lock`), que se liberan al terminar la transacción: uno por `transaction_id` y otro por documento + Dealer, siempre en ese orden para evitar *deadlocks*. Un `SELECT ... FOR UPDATE` no alcanza, porque en el caso `CREATED` todavía no existe la fila que habría que bloquear. El índice único parcial de M1 queda como red de seguridad.
- La lógica vive en `app/services/consents.py`; el router solo traduce el resultado a la respuesta HTTP.

1. Validar y normalizar.
2. **El `transaction_id` ya existe** (reenvío de la misma transacción) → **RN-04**: actualizar `full_name`, `phone` y `additional_info`, y registrar `consent_requests` (`UPDATED`). Sin enlace ni WhatsApp nuevos. Responder `200`.
   - Si ese registro tiene `is_deleted = 1` → responder `409` con el estado actual, sin modificar nada **(propuesta)**. Cuerpo: `{"detail": "La transacción ya existe y fue rechazada (NO_ACEPTADO); no se modificó ningún dato", "transaction_id": "...", "status": "NO_ACEPTADO", "is_deleted": 1}`.
   - Si el reenvío trae **otro documento u otro Dealer**, se conservan los del registro: solo se actualizan nombre, teléfono e información adicional, y se registra un log `WARNING`.
3. Buscar el **registro activo** del documento + Dealer (`is_deleted = 0`, el más reciente).
4. **Hay registro activo** (principal, pendiente o aceptado) → **RN-04**:
   - Actualizar los datos del cliente en ese registro y registrar `consent_requests` (`UPDATED`, con el nuevo `transaction_id` y la referencia al registro principal). **No** se reenvía WhatsApp y **no** se tocan sus conformidades.
   - **Registrar el nuevo ID como copia vinculada:** `consents` con el `transaction_id` recibido, `source_transaction_id` = ID del principal, documento y Dealer del principal, nombre, teléfono e información adicional de la solicitud, y **el estado, los tres checks y `responded_at` del principal tal como estén**. Sin enlace ni WhatsApp.
     - Si el principal ya fue respondido (`ACEPTADO`), la copia nace con esas respuestas, y KIZUNA recibe también la respuesta final de la copia (M5).
     - Si el principal está `PENDIENTE`, la copia también; recibe la respuesta cuando el cliente conteste en el principal (M4, paso 7).
   - Responder `200` con `affected_transaction_id` = ID del principal. Las búsquedas del registro activo usan el documento + Dealer y solo consideran principales, así que varios IDs nuevos se vinculan todos al mismo principal (nunca a otra copia).
   - El principal se bloquea con `SELECT ... FOR UPDATE`: si en paralelo el cliente está respondiendo (M4), se espera a que termine y la copia nace con la respuesta ya registrada; si la respuesta fue un rechazo, el principal deja de estar activo y se aplica el paso 5 (RN-05).
   - Un **reenvío del ID de una copia** se trata en el paso 2: se actualizan los datos del cliente de esa copia (no los del principal).
5. **No hay registro activo** (nunca existió, o solo hay registros con `is_deleted = 1` → **RN-05**) → crear `consents` con el `transaction_id` recibido (`PENDIENTE`, `is_deleted = 0`), registrar `consent_requests` (`CREATED`), crear `consent_links` con token nuevo y **enviar el WhatsApp (M3)**. Responder `201`.
6. Si falla el envío por Twilio, el registro **se conserva**, el error queda en `whatsapp_messages` y la respuesta lo indica (no es un error 5xx). Una excepción inesperada del envío también se registra como `failed` (en el log solo va el tipo de error, porque el mensaje podría incluir el teléfono).

**Detalles de implementación**

- **`additional_info` en una actualización (RN-04):** si la solicitud **no** incluye el campo, se conserva el valor anterior; si lo incluye (aunque sea `null`), se reemplaza.
- **El WhatsApp se envía dentro de la transacción**, antes del commit. Si el commit fallara después del envío, KIZUNA reintenta y se crea un enlace nuevo. Separar el envío en otra transacción sería peor: con RN-04, un reintento ya no reenviaría el WhatsApp y el cliente nunca recibiría el enlace.
- **Token del enlace:** `secrets.token_urlsafe(32)`; en `consent_links` solo se guarda el SHA-256 (`token_hash`) y `expires_at = created_at + LINK_TTL_HOURS`. El token en claro nunca se guarda ni se registra en logs.
- **Teléfono para el envío:** `whatsapp:+{PHONE_COUNTRY_PREFIX}{phone}`. Se guarda en `whatsapp_messages.to_phone` junto con `TWILIO_CONTENT_SID`.
- **Envío desacoplado:** el servicio usa la interfaz `WhatsAppSender` (`app/integrations/whatsapp.py`). Hasta M3 la implementación es `FakeWhatsAppSender`, que no envía nada: solo registra en el log (teléfono enmascarado, sin token) y responde `queued`.

**Response**
```json
{
  "transaction_id": "TRX-000123",
  "result": "CREATED",
  "affected_transaction_id": "TRX-000123",
  "status": "PENDIENTE",
  "whatsapp": { "status": "queued", "error_code": null }
}
```
- `result`: `CREATED` | `UPDATED`.
- `affected_transaction_id`: el registro que se creó o actualizó; en el paso 4 es el ID del registro principal activo (el ID recibido queda registrado como copia vinculada y se puede consultar con `GET /api/v1/consents/{transaction_id}`).
- En el paso 4, `status` es el de la copia recién creada (igual al del principal en ese momento).
- Si `result` es `UPDATED`, `whatsapp` vale `null`.

### 2.4 Endpoints de consulta (requieren API Key)

- `GET /api/v1/consents/{transaction_id}` → un registro (`404` si no existe).
- `GET /api/v1/consents?document_type=DNI&document_number=12345678&dealer=DEALER-001` → **lista** de registros de ese documento (y Dealer, opcional), ordenada por `created_at` descendente (a igual fecha, por `transaction_id` descendente). Incluye los borrados lógicamente y las copias vinculadas. Sin resultados → `200 []`. Sin paginación.
- Ambas consultas devuelven también las **copias vinculadas** (RN-04): KIZUNA puede consultar con el ID que envió y obtiene las respuestas que el cliente ya dio (o `PENDIENTE` si aún no responde). La respuesta **no** incluye `source_transaction_id`: el formato es el mismo para principales y copias.
- El documento de la consulta se normaliza igual que al registrarlo. El `transaction_id` de la ruta se compara exacto.
- Fechas en ISO 8601 UTC con sufijo `Z` (incluyen microsegundos, p. ej. `2026-10-27T06:14:17.544862Z`).
- `404 {"detail": "No existe un registro con ese ID de transacción"}` si el ID no existe.

**Response (un registro)**
```json
{
  "transaction_id": "TRX-000123",
  "document_type": "DNI",
  "document_number": "12345678",
  "dealer": "DEALER-001",
  "status": "ACEPTADO",
  "consents": {
    "personal_data": true,
    "marketing_yamaha": false,
    "marketing_dealer": true
  },
  "responded_at": "2026-11-05T15:20:11Z",
  "is_deleted": 0,
  "created_at": "2026-11-04T10:00:00Z"
}
```
- La respuesta **no** incluye nombre ni teléfono.
- Si el registro está en `NO_ACEPTADO` (`is_deleted = 1`), `consents.marketing_yamaha` y `consents.marketing_dealer` valen `null`.

### Criterios de aceptación M2

- Tests: creación (201); RN-04 con el mismo `transaction_id` (200); RN-04 con un nuevo `transaction_id` y registro activo (200, sin WhatsApp); RN-03 mismo documento con otro Dealer (201); RN-05 nueva compra tras un rechazo (201, registro y WhatsApp nuevos); validaciones (422); API Key (401) y bypass con `API_KEY_ENABLED=false`.
- Tests unitarios de normalización: tildes y Ñ conservadas, espacios colapsados, teléfono `+51 987-654-321` → `987654321`.

---

## M3 — Integración con Twilio WhatsApp

### Requerimientos

- Línea **nueva y exclusiva** en la cuenta Twilio de Yamaha, distinta del número de su chatbot, registrada como remitente de WhatsApp. El nombre visible del remitente es ⚠️ PENDIENTE-11.
- Envío mediante **Content API**: `messages.create(from_=TWILIO_WHATSAPP_FROM, to="whatsapp:+51...", content_sid=TWILIO_CONTENT_SID, content_variables=json.dumps({...}), status_callback=TWILIO_STATUS_CALLBACK_URL)`. Implementado en `app/integrations/twilio_client.py` (`TwilioWhatsAppSender`).
- **Interruptor `TWILIO_SEND_ENABLED`** (por defecto `false` en `.env.example`): con `false` se usa `FakeWhatsAppSender` y no se envía ningún mensaje real (desarrollo); con `true`, `TwilioWhatsAppSender`.
  - Con `false`, el mensaje queda en `whatsapp_messages` como `queued` sin SID (no hay un estado "simulado") y al arrancar se registra un log `WARNING`.
  - Con `true` y `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` o `TWILIO_CONTENT_SID` vacías, o con `TWILIO_WHATSAPP_FROM` sin el formato `whatsapp:+<número>`, **la aplicación no arranca** (`TwilioConfigurationError`, log `CRITICAL`) y el despliegue falla.
  - Sin `TWILIO_STATUS_CALLBACK_URL` la aplicación arranca con un log `WARNING`: el mensaje se envía sin `status_callback` y su estado no avanza de `queued`.
- **Detalles del envío:**
  - El SDK de Twilio es síncrono: se ejecuta en un hilo (`asyncio.to_thread`) con timeout `TWILIO_TIMEOUT_SECONDS`, porque el envío ocurre dentro de la transacción de `POST /api/v1/consents` (con el bloqueo del cliente tomado).
  - Un error de Twilio (`TwilioRestException`) se guarda como `failed` con su `error_code` y su mensaje original (sin el prefijo `"Unable to create record: "` que agrega el SDK). Un error sin respuesta de Twilio (red, timeout, SSL) se guarda como `failed` sin código. En ningún caso se devuelve 5xx.
  - Los estados que Twilio puede devolver al crear el mensaje y que no están en `whatsapp_status` (`accepted`, `sending`, `scheduled`) se guardan como `queued`.
  - En el log no van el token ni el teléfono completo; solo el `transaction_id`, el teléfono enmascarado, el SID, el estado y el código de error.
- **Plantilla con botón** (decisión): tipo de contenido **Call to Action** (`twilio/call-to-action`) de Twilio Content API.
  - **Cuerpo** (texto final lo define Yamaha **antes** de crear la plantilla; texto base):
    > Estimado cliente, `<MENSAJE_POR_DEFINIR>` Ingrese al siguiente enlace para firmar el consentimiento.
  - **Botón URL:** etiqueta **"Revisar autorizaciones"** → `https://<DOMINIO_LANDING>/?t={{1}}`.
  - Meta exige que el **dominio del botón sea fijo** en la plantilla y que solo el final de la URL sea variable. Por eso el **dominio final de la landing (PENDIENTE-15) debe estar definido antes de crear la plantilla**.
  - Variable de plantilla: `{{1}}` = token del enlace. Al enviar: `content_variables={"1": token}`.
  - El mockup `./assets/mockup-whatsapp.png` muestra la URL dentro del texto; queda reemplazado por el botón.
  - **Orden de trabajo:** (1) Yamaha cierra el texto final → (2) se define el dominio → (3) se crea la plantilla en Twilio → (4) aprobación de Meta → (5) se configura `TWILIO_CONTENT_SID`.
- El token se genera con `secrets.token_urlsafe(32)` (seguro para URL). La URL completa que verá el cliente es `{LANDING_BASE_URL}/?t={token}`.
- Compatible con Android e iOS (lo garantiza WhatsApp; no hay desarrollo nativo).

### Webhook `POST /api/v1/webhooks/twilio/status`

- Recibe los callbacks de estado de Twilio (`application/x-www-form-urlencoded`): `MessageSid`, `MessageStatus`, `ErrorCode`, `ErrorMessage`. Implementado en `app/api/v1/webhooks.py` y `app/services/whatsapp_status.py`. **No usa API Key**: lo protege la firma.
- **Valida la firma** `X-Twilio-Signature` con `twilio.request_validator.RequestValidator` y `TWILIO_AUTH_TOKEN`, sobre todos los parámetros recibidos. La validación se puede apagar con `TWILIO_SIGNATURE_VALIDATION_ENABLED=false` (solo para local, **(propuesta)**).
  - Sin header → `403 {"detail": "Falta la firma de Twilio"}`. Firma inválida → `403 {"detail": "Firma de Twilio inválida"}`. En ambos casos no se guarda nada y se registra un `WARNING` (sin la firma ni el payload).
  - La firma se valida contra `TWILIO_STATUS_CALLBACK_URL` (la URL que Twilio firma) y, si no coincide, contra la URL de la petición. Detrás de Cloud Run la aplicación ve la petición como `http` interno, por eso la URL configurada es la principal.
  - Con la validación encendida y `TWILIO_AUTH_TOKEN` vacío se rechazan todos los callbacks y al arrancar se registra un `WARNING`. Con la validación apagada también se registra un `WARNING` al arrancar. **En ninguno de los dos casos se impide el arranque**, tampoco con `APP_ENV=production` (decisión del 27/10/2026, puede revisarse).
- Guarda el evento en `whatsapp_message_events` y actualiza `whatsapp_messages.status`, `error_code` y `error_message` (este último truncado a 1000 caracteres).
- **El estado no retrocede.** Twilio puede entregar los callbacks desordenados. Todos se guardan como eventos, pero `whatsapp_messages.status` solo cambia si el estado nuevo está más avanzado: `queued` → `sent` → `delivered` / `failed` / `undelivered` → `read`. `delivered`, `failed` y `undelivered` tienen el mismo nivel: un `delivered` atrasado no cambia un `failed` ni al revés, y uno atrasado no deshace un `read`.
- Los estados intermedios `accepted`, `sending` y `scheduled` se registran como `queued`, igual que al crear el mensaje.
- Es **idempotente**: un evento por mensaje y estado (UNIQUE + `INSERT ... ON CONFLICT DO NOTHING`). Un callback repetido, aunque lleguen dos a la vez, no crea otro evento ni vuelve a modificar el mensaje.
- Responde `204` sin cuerpo, también cuando el callback se ignora: repetido, con un `MessageSid` que no es de este sistema o con un estado que `whatsapp_status` no contempla (p. ej. `canceled`). Los dos últimos casos registran un `WARNING`. Si un callback llegara antes del commit de `POST /api/v1/consents` se ignoraría como SID desconocido; los callbacks siguientes corrigen el estado.
- Sin `MessageSid` o `MessageStatus` → `422` en español (mismo formato que el resto de la API).
- Pruebas locales con **ngrok** apuntando `TWILIO_STATUS_CALLBACK_URL` al túnel.

### Criterios de aceptación M3

- Un mensaje real llega a un número de prueba con el botón "Revisar autorizaciones", el botón abre la landing con el token correcto y el estado avanza `queued → sent → delivered → read` en la base de datos.
- Un envío a un número inválido registra `failed` o `undelivered` con su `error_code`.
- Tests con el cliente de Twilio simulado (mock).

---

## M4 — API pública para la landing (RN-01)

### `GET /api/v1/public/consent-links/{token}` — validar enlace

Busca por `sha256(token)`.

**Response 200**
```json
{
  "link_status": "VALID",
  "privacy_policy_url": "https://...",
  "dealer_display_name": null
}
```
- Implementado en `app/api/v1/public.py` y `app/services/consent_links.py`. Los endpoints públicos **no usan API Key**.
- `link_status`: `VALID` | `EXPIRED` (`now > expires_at`) | `USED` (`used_at` no nulo). Un enlace usado y además vencido es `USED` (el cliente ya respondió). Un enlace vencido o usado responde `200` con su `link_status`.
- Token inexistente → `404 {"detail": "El enlace no existe"}` (la landing lo trata igual que un enlace inválido).
- `privacy_policy_url` vale `null` si `PRIVACY_POLICY_URL` no está configurada (PENDIENTE-14).
- ⚠️ PENDIENTE-05: ¿se muestra el nombre del Dealer en la landing? El mockup dice "DEALER" genérico. Mientras tanto `dealer_display_name` vale `null` y la landing muestra "DEALER".
- **No** devuelve datos personales.

### `POST /api/v1/public/consent-links/{token}/submit` — registrar respuestas

**Request**
```json
{ "personal_data": true, "marketing_yamaha": false, "marketing_dealer": true }
```

- Los tres campos son **obligatorios** y deben ser booleanos de JSON (`true`/`false`): `"true"`, `1` o `null` se rechazan, igual que los campos extra → `422` en español. Es evidencia legal: no se interpreta el valor.

**Lógica (en una sola transacción de base de datos)**
1. Validar el enlace: si es `EXPIRED` o `USED` → `410 Gone` con `{"detail", "link_status"}`: `"El enlace venció"` (`EXPIRED`) o `"El enlace ya fue utilizado"` (`USED`). Token inexistente → `404`. En ningún caso se modifica nada.
2. Bloquear la fila (`SELECT ... FOR UPDATE`) para evitar doble envío. **El bloqueo se toma antes de validar el paso 1**: así, de dos envíos simultáneos el segundo espera, ve `used_at` y recibe `410 USED`. Después se bloquea también la fila del registro principal (la misma que bloquea `POST /api/v1/consents` antes de crear una copia vinculada).
3. Registrar `consent_evidence` (IP, user agent, versión y URL de la política, respuestas, fecha).
   - `answers` guarda **exactamente lo que envió el cliente**, también el marketing marcado en un rechazo (RN-01 anula el marketing en `consents`, no en la evidencia).
   - IP: la **primera** de `X-Forwarded-For` (se mantiene así para no depender de la nube de despliegue; en Cloud Run ese valor lo puede escribir el propio cliente). Sin el header, la IP de la conexión (local). Si no es una IP válida se guarda `null` y la respuesta se registra igual.
   - `user_agent` tal cual (o `null` si no viene). `policy_url` guarda `""` si `PRIVACY_POLICY_URL` no está configurada (PENDIENTE-14).
   - `submitted_at`, `responded_at`, `used_at` y `deleted_at` (en RN-01) llevan el mismo instante.
4. Si `personal_data = true` → `status = ACEPTADO` y se guardan los 3 valores.
5. Si `personal_data = false` → **RN-01** (prevalece; se acepta el envío con todos los checks desmarcados):
   - `status = NO_ACEPTADO`, `consent_personal_data = false`.
   - **Borrado lógico:** `full_name = null`, `is_deleted = 1`, `deleted_at = now()`.
   - `consent_marketing_yamaha = null` y `consent_marketing_dealer = null` (RN-01: no se conservan las respuestas de marketing).
   - Se conservan: `transaction_id`, `document_type`, `document_number`, `dealer`, `phone` (por trazabilidad), `additional_info`, `consent_evidence` y `whatsapp_messages`.
6. `responded_at = now()` y `consent_links.used_at = now()`.
7. **Copias vinculadas (RN-04):** con la fila del principal bloqueada, copiar la respuesta a sus copias vinculadas que sigan `PENDIENTE` con `apply_response_to_linked_copies()` (`app/services/linked_copies.py`, ya implementada en M2). En `ACEPTADO` se copian el estado, los tres checks y `responded_at`; en `NO_ACEPTADO` se aplica RN-01 a cada copia (`is_deleted = 1`, `deleted_at`, `full_name = null`, marketing en `null`; se conservan documento, Dealer, teléfono y transacción).
8. Crear en `kizuna_notifications` (`PENDIENTE`) **un registro por el principal y uno por cada copia vinculada actualizada en el paso 7**, y disparar los envíos (M5) **después del commit**.
   - Implementado en `app/services/kizuna_notifications.py`: `enqueue_notifications()` crea las filas con el payload de `build_kizuna_payload()` (`app/integrations/kizuna_client.py`) y `target_url = KIZUNA_CALLBACK_URL`. El router dispara `KizunaNotifier.dispatch()` como `BackgroundTask` (envío y reintentos: M5).
9. Responder `200 {"status":"ACEPTADO"|"NO_ACEPTADO"}`. La landing redirige a `/gracias` en ambos casos.

### Criterios de aceptación M4

- Tests: aceptación completa; aceptación con marketing en `false`; RN-01 con los tres checks desmarcados (verificar `full_name = null`, `marketing_* = null`, `is_deleted = 1` y documento, Dealer y transacción conservados); enlace vencido (410); enlace usado (410); doble envío concurrente (solo uno se registra).
- Copias vinculadas: la respuesta del principal se copia a sus copias `PENDIENTE` (aceptación y RN-01) y se crea una notificación a KIZUNA por cada ID de transacción.

---

## M5 — Notificación a KIZUNA (outbox y reintentos)

### Requerimientos

- Tras el envío del cliente (M4), el backend hace `POST` a `KIZUNA_CALLBACK_URL` con la respuesta final.
- **Una petición por cada ID de transacción:** si el registro tiene copias vinculadas (RN-04), se envía una respuesta para el principal y otra por cada copia, cada una con su propio `transaction_id` y su propia fila en `kizuna_notifications` (reintentos independientes).
- **Copia que nace ya con respuesta** (el principal estaba `ACEPTADO` cuando llegó el ID nuevo, M2 paso 4): **también se le envía su respuesta final a KIZUNA**, aunque el cliente no vuelva a responder, para que KIZUNA reciba la respuesta de cada ID. En M5, `POST /api/v1/consents` crea esa notificación en `kizuna_notifications` (`PENDIENTE`) en la misma transacción en que crea la copia, y dispara el envío **después del commit**. Una copia que nace `PENDIENTE` no se notifica al crearse (se notifica cuando el cliente responde, M4 pasos 7 y 8), ni tampoco el reenvío del mismo `transaction_id` (M2 paso 2).
- Implementado en `app/integrations/kizuna_client.py` (`build_kizuna_payload()`, `KizunaClient`: un intento) y `app/services/kizuna_notifications.py` (`KizunaNotifier`: outbox, reclamo, reintentos y reproceso).
- **Interruptor:** `KIZUNA_CALLBACK_ENABLED=true|false`. Con `false`, **no se dispara ninguna petición**: la notificación se guarda en `kizuna_notifications` con estado `PENDIENTE` y se registra en el log que el envío está desactivado. Por defecto en `.env.example`: `false`.
- **Mientras no exista el endpoint real de KIZUNA**, el envío apunta a un **mock** (ver más abajo). Pasar al endpoint real debe requerir **cambios mínimos**: la URL y la autenticación por variables de entorno, y los campos en **una sola función** de mapeo (`build_kizuna_payload()` en `integrations/kizuna_client.py`).
- **Reintentos:** 1 intento inicial + **hasta 3 reintentos** (`KIZUNA_CALLBACK_MAX_RETRIES=3`), con espera configurable entre intentos (`KIZUNA_CALLBACK_BACKOFF_SECONDS`, por ejemplo `5,30,120`) y timeout por intento (`KIZUNA_CALLBACK_TIMEOUT_SECONDS`).
  - La espera `KIZUNA_CALLBACK_BACKOFF_SECONDS[i]` va antes del reintento `i+1`. Si hay más reintentos que valores, se repite el último; sin valores, no hay espera. El header `Retry-After` de un `429` no se usa.
  - `KIZUNA_CALLBACK_TIMEOUT_SECONDS` limita el **intento completo** (no solo cada etapa de la conexión).
- Se reintenta ante error de red, timeout o respuesta `5xx`/`429`. Ante otros `4xx` se marca `FALLIDO` sin reintentar **(propuesta)**. Éxito = cualquier `2xx`. Un `3xx` no se sigue y se marca `FALLIDO` sin reintentar.
- Cada intento actualiza `kizuna_notifications` (`attempts`, `last_http_status`, `last_error`, `last_attempt_at`) con su propio commit: durante el envío y la espera entre intentos no queda ninguna transacción abierta. Si se agotan los reintentos → `FALLIDO` y log `ERROR`. El endpoint de consulta (M2) sigue siendo la vía de respaldo.
  - `last_error`: `HTTP <código>: <cuerpo>` (cuerpo truncado a 500 caracteres; puede traer el documento, por eso **no** va al log) o el tipo de error de red o timeout. En un envío exitoso queda en `null`.
  - En el log solo van el `transaction_id`, el ID de la notificación, el intento y el código HTTP: nunca el payload ni el valor de autenticación.
- La notificación guarda su `payload` y su `target_url` al crearse: se envía eso aunque después cambie la variable de entorno.
- Las notificaciones de un mismo envío (el principal y sus copias) se envían **en paralelo**: los reintentos de una no retrasan a las demás.
- Ejecución: `BackgroundTasks` de FastAPI después del commit. En Cloud Run se requiere **CPU siempre asignada** (M7) para que la tarea no se congele. *(Alternativa si se requiere más robustez: Cloud Tasks.)*
- **Reclamo (una sola instancia envía cada notificación):** antes de enviar, la notificación se reclama con `SELECT ... FOR UPDATE SKIP LOCKED` en una transacción corta, que marca `kizuna_notifications.claimed_at`. El reclamo se renueva en cada intento. Una notificación reclamada por otra instancia (con el reclamo vigente) no se envía. Con `KIZUNA_CALLBACK_ENABLED=false` no se reclama.
- **Reproceso al arrancar** (decisión del 27/10/2026): si `KIZUNA_CALLBACK_ENABLED=true`, al arrancar la app se reclaman y envían en segundo plano (sin retrasar el arranque; de la más antigua a la más reciente, hasta 10 a la vez) todas las notificaciones `PENDIENTE`:
  - **sin reclamar**: todas, antiguas y recientes, incluidas las acumuladas mientras el envío estaba apagado;
  - con el **reclamo vencido**: más de `KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES` (por defecto 10) sin intento, porque la instancia que las enviaba se reinició. Conservan sus intentos anteriores.
  - Al apagar la app, el reproceso en curso se cancela; lo que quede conserva su reclamo y se retoma cuando venza.
  - `FALLIDO` y `ENVIADO` no se reprocesan.
- **Arranque:** con `KIZUNA_CALLBACK_ENABLED=false` → log `WARNING`. Con `true` y sin `KIZUNA_CALLBACK_URL`, o con `KIZUNA_CALLBACK_AUTH_HEADER` sin `KIZUNA_CALLBACK_AUTH_VALUE` → **la aplicación no arranca** (`KizunaConfigurationError`). Si `KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES` no supera la espera más larga entre intentos más el timeout → log `WARNING` (otra instancia podría reclamar una notificación a mitad de sus reintentos y KIZUNA la recibiría dos veces).

### Contrato del callback — ⚠️ PENDIENTE-13 (lo define Yamaha/KIZUNA)

Falta: URL, método de autenticación, formato del payload, literales de estado (`NO ACEPTADO` con espacio o `NO_ACEPTADO`) y respuesta esperada. **Mientras tanto** se implementa este payload, construido desde una sola función para cambiarlo fácilmente:
```json
{
  "transaction_id": "TRX-000123",
  "document_type": "DNI",
  "document_number": "12345678",
  "dealer": "DEALER-001",
  "status": "ACEPTADO",
  "consents": {
    "personal_data": true,
    "marketing_yamaha": false,
    "marketing_dealer": true
  },
  "responded_at": "2026-11-05T15:20:11Z"
}
```
Autenticación saliente parametrizada con `KIZUNA_CALLBACK_AUTH_HEADER` y `KIZUNA_CALLBACK_AUTH_VALUE`. En `NO_ACEPTADO`, `marketing_*` vale `null`.

### Mock de KIZUNA (`mocks/kizuna_mock/`)

- Mini aplicación FastAPI levantada con `docker compose` (servicio `kizuna-mock`, puerto `8081`).
- `POST /kizuna/consent-result`: valida que el payload tenga la estructura esperada, lo registra en el log (enmascarando el documento) y responde `200 {"received": true}`.
- Para simular fallos y probar los reintentos: `MOCK_FAIL_TIMES=N` (responde `500` las primeras N veces) y `MOCK_RESPONSE_DELAY_SECONDS` (simula un timeout).
- `GET /kizuna/received`: lista los payloads recibidos (útil en pruebas).
- Valor local: `KIZUNA_CALLBACK_URL=http://localhost:8081/kizuna/consent-result` si la API corre en el host (`uv run uvicorn`), o `http://kizuna-mock:8081/kizuna/consent-result` si corre dentro de docker compose (el compose lo fija).
- `DELETE /kizuna/received`: reinicia el contador y la lista de payloads (útil entre pruebas).
- El mock **no se despliega** en Producción.

### Criterios de aceptación M5

- Con `KIZUNA_CALLBACK_ENABLED=false` no sale ninguna petición HTTP y la notificación queda `PENDIENTE`.
- Con el mock en `MOCK_FAIL_TIMES=3` → `ENVIADO` con `attempts = 4`.
- Con el mock fallando siempre → `FALLIDO` con `attempts = 4`.
- Cambiar al endpoint real solo requiere modificar variables de entorno y `build_kizuna_payload()`.

---

## M6 — Frontend (Astro)

### Requerimientos generales

- Astro con `output: 'static'`, TypeScript, sin frameworks de UI. El JavaScript del formulario va en un `<script>` de la página.
- Diseño **mobile-first** y responsive (también para escritorio), fiel a `./assets/mockup-landing.png`: header con "YAMAHA" y "Privacidad y consentimiento", fondo gris claro y botón azul de ancho completo.
- Variables de build: `PUBLIC_API_BASE_URL` y `PUBLIC_PRIVACY_POLICY_URL` (⚠️ PENDIENTE-14: URL real de la política de privacidad).
- Accesibilidad: `<label>` asociados a los checkboxes, foco visible y contraste AA.

### Páginas

**1. `/` — Landing (`/?t={token}`)**
- Al cargar: si no hay token, ir a `/enlace-invalido`. Si lo hay, `GET /public/consent-links/{token}`; si responde `EXPIRED`, `USED` o `404`, ir a `/enlace-invalido` (con motivo por query, por ejemplo `?motivo=vencido`).
- Mientras valida, mostrar un indicador de carga.
- Textos (del mockup, tal cual):
  - Etiqueta: **AUTORIZACIÓN DE DATOS**
  - Título: **Tu privacidad es importante**
  - Intro: "Antes de continuar, revisa la política de privacidad y selecciona las autorizaciones que deseas otorgar."
  - Check 1: "Acepto la [política de privacidad]" (sin asterisco, ya que se quitó la nota de obligatoriedad)
  - Check 2: "Autorizo a **YAMAHA** para el tratamiento de mis datos personales con fines de perfilamiento y envío de publicidad, de acuerdo con la [política de privacidad]."
  - Check 3: "Autorizo al **DEALER** para el tratamiento de mis datos personales con fines de perfilamiento y envío de publicidad, de acuerdo con la [política de privacidad]."
  - Botón: **ENVIAR**
- Los checkboxes inician **desmarcados**. Marcado = aceptado; desmarcado = rechazado.
- **RN-01 prevalece:** se **elimina** del mockup la frase "* Autorización obligatoria para continuar". El botón ENVIAR está **siempre habilitado**. El cliente puede enviar con cualquier combinación de checks, **incluso todos desmarcados**, y así se registra su rechazo. No hay validación que bloquee el envío ni diálogo de confirmación.
- Al enviar: deshabilitar el botón (evitar doble clic), `POST /submit` y, si la respuesta es exitosa (`ACEPTADO` o `NO_ACEPTADO`), ir a `/gracias`. Ante `410`, ir a `/enlace-invalido`. Ante un error de red, mostrar un mensaje y permitir reintentar.

**2. `/gracias` — Agradecimiento** (diseño y texto: propuesta del responsable)
- Texto propuesto: título "¡Gracias por tu respuesta!" y cuerpo "Hemos registrado tus autorizaciones correctamente. Ya puedes cerrar esta ventana."
- Mismo header y estilo que la landing; ícono de confirmación.

**3. `/enlace-invalido`**
- Texto propuesto: "Este enlace ya no está disponible" y, según el motivo, "El enlace ha vencido" o "Ya registraste tus respuestas anteriormente".

### Hosting de la landing: Firebase Hosting (decisión)

- Firebase Hosting en el **mismo proyecto de Google Cloud** de Yamaha (la región del backend y la base de datos no afecta a Firebase Hosting, que sirve el sitio por CDN global).
- `firebase.json`: `public: "dist"` y `cleanUrls: true` para que `/gracias` y `/enlace-invalido` funcionen sin `.html`.
- Cabeceras de seguridad en `firebase.json` **(propuesta)**: `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (evita filtrar el token `?t=` a sitios externos como la política de privacidad).
- Despliegue manual: `npm run build` + `firebase deploy --only hosting`.
- Dominio: se coordina con Yamaha, probablemente provisto desde **Hostinger** (⚠️ PENDIENTE-15). Se conecta como dominio personalizado de Firebase.

### Criterios de aceptación M6

- Se ve correctamente en anchos de 360 px, 768 px y 1280 px.
- Los flujos funcionan contra el backend local: válido → gracias; todos los checks desmarcados → gracias (registrado como `NO_ACEPTADO`); vencido; usado.
- Lighthouse ≥ 90 en Performance y Accessibility (móvil).

---

## M7 — Infraestructura en Google Cloud y despliegue manual

> La **región** la decide Yamaha (⚠️ PENDIENTE-09) y se usa en todos los recursos. Los accesos los provee Yamaha.

### Recursos

| Recurso | Configuración |
|---|---|
| APIs a habilitar | Cloud Run, Cloud SQL Admin, Secret Manager, Artifact Registry, Cloud Logging, Firebase Hosting |
| Cloud SQL | PostgreSQL 16; tier pequeño (por ejemplo `db-g1-small` o equivalente, a validar con Yamaha; los tiers de núcleo compartido requieren `--edition=ENTERPRISE`); backups automáticos activados; base `consents_db`. Dos usuarios: `consent_owner` (dueño del esquema, solo para migraciones) y `consent_app` (el de la aplicación, creado por SQL con solo `SELECT/INSERT/UPDATE/DELETE` sobre las tablas y `ALTER DEFAULT PRIVILEGES` para las tablas futuras). Los usuarios creados con `gcloud` reciben `cloudsqlsuperuser`, por eso el de la aplicación se crea por SQL |
| Secret Manager | `DATABASE_URL` (URL completa con el usuario `consent_app`, incluye la contraseña), `API_KEY`, `TWILIO_AUTH_TOKEN`, `KIZUNA_CALLBACK_AUTH_VALUE` (este último solo cuando PENDIENTE-13 defina la autenticación de KIZUNA). Se cargan por la entrada estándar (`--data-file=-`) y se montan con `:latest` |
| Artifact Registry | Repositorio Docker `consent-api` |
| Service Account | `consent-api-sa` con roles `Cloud SQL Client` y `Secret Manager Secret Accessor` |
| Cloud Run `consent-api` | `--min-instances=1`, `--no-cpu-throttling` (CPU siempre asignada, por M5), `--add-cloudsql-instances`, `--set-secrets`, ingreso público (lo usan KIZUNA y Twilio) |
| Firebase Hosting | Se agrega Firebase al **mismo proyecto de Google Cloud**; sitio `consent-landing`; dominio propio verificado con registros DNS en Hostinger; SSL automático |

### Imagen del backend (`Dockerfile`)

- Dos etapas: `python:3.12-slim` con uv (versión fija) instala solo las dependencias de producción de `uv.lock` (`uv sync --frozen --no-dev`); la imagen final, también `python:3.12-slim`, no lleva uv.
- Corre como usuario sin privilegios (`appuser`, uid 10001); el código queda de root y la aplicación no puede modificarlo.
- Incluye `alembic/` y `alembic.ini` (las migraciones pueden correrse desde la imagen). No incluye `.env`, tests, mocks, `scripts/`, `postman/` ni `deploy/` (`.dockerignore`).
- `uvicorn` escucha en `$PORT` (8080 por defecto), con `exec` para recibir el `SIGTERM` de Cloud Run, y con `--proxy-headers --forwarded-allow-ips '*'` para ver `https` detrás del proxy de Cloud Run (validación de la firma de Twilio).

### Variables de entorno de Cloud Run (`deploy/cloudrun.env.yaml`)

- Plantilla con **todas** las variables no secretas de la sección 9, para `--env-vars-file` (no `--set-env-vars`, que separa por comas y rompería `CORS_ALLOWED_ORIGINS` y `KIZUNA_CALLBACK_BACKOFF_SECONDS`). Los valores `<...>` se reemplazan antes de desplegar. Un test verifica que no falte ninguna variable y que no incluya secretos.
- Valores iniciales: `APP_ENV=production`, `API_KEY_ENABLED=true`, `TWILIO_SIGNATURE_VALIDATION_ENABLED=true`, `TWILIO_SEND_ENABLED=false` (hasta que la plantilla esté aprobada por Meta y exista `TWILIO_CONTENT_SID`; PENDIENTE-07 y 15), `KIZUNA_CALLBACK_ENABLED=false` (paso 5) y `DOCS_ENABLED=true` (valor por defecto; ⚠️ PENDIENTE-12).

### Pasos de despliegue manual (documentar en el `README.md` de cada repositorio)

1. `docker build --platform linux/amd64` y `docker push` de la imagen a Artifact Registry (o `gcloud builds submit`).
2. Ejecutar migraciones **antes** del deploy: Cloud SQL Auth Proxy local + `alembic upgrade head` con el usuario `consent_owner`.
3. `gcloud run deploy consent-api --image ... --region <REGION> --service-account consent-api-sa --min-instances=1 --no-cpu-throttling --add-cloudsql-instances <INSTANCE> --allow-unauthenticated --env-vars-file deploy/cloudrun.env.yaml --set-secrets ...`
4. Configurar en Twilio el `status_callback` con la URL de Cloud Run.
5. Mantener `KIZUNA_CALLBACK_ENABLED=false` hasta tener el endpoint real de KIZUNA y su contrato confirmado.
6. Landing: `npm run build` con las variables `PUBLIC_*` de Producción y `firebase deploy --only hosting`.
7. En Firebase Hosting, agregar el dominio personalizado, crear en Hostinger los registros DNS (TXT de verificación y A) que indique Firebase y esperar a que el SSL quede activo.
8. Ajustar `CORS_ALLOWED_ORIGINS` y `LANDING_BASE_URL` al dominio final.

### Criterios de aceptación M7

- `GET https://<cloud-run-url>/health` → 200.
- La landing carga por HTTPS en el dominio final.
- Ningún secreto queda en el código, en la imagen ni en variables de entorno en texto plano.

---

## M8 — Pruebas de punta a punta en Producción y entrega

1. Usar un **Dealer de prueba** (por ejemplo `DEALER-TEST`) y números de teléfono internos.
2. Casos:
   - Alta → WhatsApp → aceptación total → callback a KIZUNA.
   - Alta → envío con todos los checks desmarcados (RN-01) → verificar borrado lógico (`is_deleted = 1`, nombre nulo) y callback.
   - RN-04: reenvío del mismo documento y Dealer con registro activo → solo se actualizan los datos.
   - RN-04 con un ID nuevo mientras el cliente no responde → la copia queda `PENDIENTE`; al responder el cliente, la copia recibe la respuesta y KIZUNA recibe dos callbacks (uno por ID).
   - RN-03: mismo documento con otro Dealer → registro y WhatsApp nuevos.
   - RN-05: nueva transacción del mismo documento y Dealer tras un rechazo → registro y WhatsApp nuevos.
   - Enlace vencido (bajar temporalmente `LINK_TTL_HOURS`) y enlace usado.
   - Número inválido → error registrado.
   - Callback a KIZUNA caído → reintentos y estado `FALLIDO`.
3. **Eliminar los datos de prueba** antes de la salida en vivo (script SQL documentado).
4. Entregables: repositorios, `README.md` con instalación local y despliegue, colección **Postman** (endpoints de KIZUNA y públicos), Swagger exportado (`openapi.json`) para el equipo de KIZUNA y listado de variables de entorno.

---

## 9. Variables de entorno

### Backend

| Variable | Ejemplo / por defecto | Descripción |
|---|---|---|
| `APP_ENV` | `local` / `production` | |
| `LOG_LEVEL` | `INFO` | |
| `DOCS_ENABLED` | `true` | Swagger en `/docs` (⚠️ PENDIENTE-12) |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/consents_db` | **URL completa, incluida la contraseña** (no hay una variable `DATABASE_PASSWORD` aparte). Es un secreto. En local se define en `.env`; en Cloud Run se monta desde Secret Manager y usa el socket de Cloud SQL (`postgresql+asyncpg://user:pass@/consents_db?host=/cloudsql/<INSTANCE>`) |
| `API_KEY_ENABLED` | `true` | `false` desactiva la validación (pruebas locales) |
| `API_KEY_HEADER` | `X-API-Key` | |
| `API_KEY` | *(secreto)* | Obligatoria si `API_KEY_ENABLED=true`: si está vacía, la aplicación no arranca |
| `CORS_ALLOWED_ORIGINS` | `https://consentimiento.dominio.com` | Lista separada por comas |
| `LINK_TTL_HOURS` | `168` | Vigencia del enlace en **horas** (1 semana) |
| `LANDING_BASE_URL` | `https://consentimiento.dominio.com` | Base del enlace enviado por WhatsApp |
| `PRIVACY_POLICY_URL` | ⚠️ PENDIENTE-14 | |
| `PRIVACY_POLICY_VERSION` | `v1` | Se guarda en la evidencia |
| `TWILIO_SEND_ENABLED` | `false` | Enciende o apaga el envío real de WhatsApp. Con `false` no se llama a Twilio: el envío solo se registra en el log y el mensaje queda como `queued` sin SID (desarrollo). Con `true` son obligatorias `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` y `TWILIO_CONTENT_SID` |
| `TWILIO_ACCOUNT_SID` | | |
| `TWILIO_AUTH_TOKEN` | *(secreto)* | |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+1XXXXXXXXXX` | Número nuevo y exclusivo |
| `TWILIO_CONTENT_SID` | `HXxxxxxxxx` | Plantilla Call to Action aprobada (botón "Revisar autorizaciones") |
| `TWILIO_STATUS_CALLBACK_URL` | `https://<api>/api/v1/webhooks/twilio/status` | |
| `TWILIO_SIGNATURE_VALIDATION_ENABLED` | `true` | (propuesta) `false` apaga la validación de `X-Twilio-Signature` en el webhook (solo para local). Apagada, o encendida con `TWILIO_AUTH_TOKEN` vacío, la aplicación arranca con un `WARNING` |
| `TWILIO_TIMEOUT_SECONDS` | `10` | Timeout de cada envío a Twilio (el envío ocurre dentro de la transacción de `POST /api/v1/consents`) |
| `PHONE_COUNTRY_PREFIX` | `51` | Se antepone al enviar |
| `KIZUNA_CALLBACK_ENABLED` | `false` | Enciende o apaga el envío final a KIZUNA. Con `false` no sale ninguna petición |
| `KIZUNA_CALLBACK_URL` | `http://kizuna-mock:8081/kizuna/consent-result` (local) | Endpoint real: ⚠️ PENDIENTE-13 |
| `KIZUNA_CALLBACK_AUTH_HEADER` | ⚠️ PENDIENTE-13 | |
| `KIZUNA_CALLBACK_AUTH_VALUE` | *(secreto)* | |
| `KIZUNA_CALLBACK_MAX_RETRIES` | `3` | Reintentos después del primer intento |
| `KIZUNA_CALLBACK_BACKOFF_SECONDS` | `5,30,120` | Espera antes de cada reintento |
| `KIZUNA_CALLBACK_TIMEOUT_SECONDS` | `10` | Límite de cada intento completo |
| `KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES` | `10` | Al arrancar se reenvían las notificaciones `PENDIENTE` sin reclamar y las que llevan más de N minutos sin intento (reclamo vencido) |

### Frontend (build)

| Variable | Descripción |
|---|---|
| `PUBLIC_API_BASE_URL` | URL base del backend (Cloud Run) |
| `PUBLIC_PRIVACY_POLICY_URL` | URL de la política de privacidad |

---

## 10. Pendientes por confirmar

Los puntos resueltos se conservan con su ID para mantener la trazabilidad (detalle en la sección 11).

| ID | Tema | Pregunta | Mientras tanto |
|---|---|---|---|
| PENDIENTE-05 | Dealer | ¿KIZUNA envía código, nombre o ambos? ¿Se muestra el nombre en la landing? | Un campo `dealer`; la landing muestra "DEALER" |
| PENDIENTE-07 | Texto final del mensaje | Yamaha lo cerrará antes de construir la plantilla en Twilio | Bloquea la creación de la plantilla |
| PENDIENTE-09 | Región de Google Cloud | La define Yamaha | Parámetro `<REGION>` en los comandos |
| PENDIENTE-10 | Teléfonos extranjeros | Con la regla de 9 dígitos sin prefijo, solo se soportan móviles de Perú. ¿Es correcto? | Se rechaza (422) lo que no tenga 9 dígitos |
| PENDIENTE-11 | Nombre visible del remitente | Nombre que verá el cliente en WhatsApp (el mockup muestra "YAMAHA***") | — |
| PENDIENTE-12 | Swagger en Producción | ¿Se deja `/docs` expuesto? | Controlado por `DOCS_ENABLED` |
| PENDIENTE-13 | Contrato del callback a KIZUNA | URL, autenticación, payload, literales de estado y respuesta esperada | Envío al **mock** con el payload propuesto en M5; `KIZUNA_CALLBACK_ENABLED=false` |
| PENDIENTE-14 | Política de privacidad | URL y versión vigentes | Variables de entorno |
| PENDIENTE-15 | Dominio de la landing | Dominio o subdominio final (probablemente provisto desde Hostinger). **Se requiere antes de crear la plantilla de WhatsApp**, porque el botón usa un dominio fijo | `LANDING_BASE_URL` y `CORS_ALLOWED_ORIGINS` configurables |
| PENDIENTE-16 | Expiración sin respuesta | Si el enlace vence sin respuesta, ¿se notifica algo a KIZUNA? | No se notifica; el estado queda `PENDIENTE` |

---

## 11. Registro de decisiones

| Fecha | Punto | Decisión |
|---|---|---|
| 26/10/2026 | PENDIENTE-01 · Check 1 vs mockup | **Prevalece RN-01**: el cliente puede enviar sin marcar los checks y así se registra su rechazo. |
| 26/10/2026 | PENDIENTE-02 · Alcance del borrado lógico | Solo se borra el **nombre** del cliente. Se agrega `is_deleted` (`SMALLINT`, 0/1, por defecto 0; 1 = borrado). La evidencia y los mensajes de WhatsApp no se tocan. |
| 26/10/2026 | PENDIENTE-03 · Nueva compra tras un rechazo | Se envía un **nuevo WhatsApp** y se crea un **nuevo registro** (RN-05). Documento + Dealer **no es único**; la **única PK es el ID de transacción**. |
| 26/10/2026 | PENDIENTE-06 · Formato del enlace en WhatsApp | **Botón** "Revisar autorizaciones" hacia la URL completa (plantilla Call to Action). |
| 26/10/2026 | PENDIENTE-07 · Texto del mensaje | Yamaha cerrará el texto final **antes** de construir la plantilla en Twilio. |
| 26/10/2026 | PENDIENTE-13 · Callback a KIZUNA | Mientras no exista el endpoint real, se envía a un **mock** y luego solo se ajustan la URL y los campos. Variable `KIZUNA_CALLBACK_ENABLED` para encender o apagar el envío. |
| 26/10/2026 | Arquitectura | **Landing, backend y base de datos** se despliegan en **Google Cloud**. |
| 26/10/2026 | Callback a KIZUNA | El backend notifica a KIZUNA con hasta **3 reintentos** y además expone un endpoint de **consulta a demanda**. |
| 26/10/2026 | Seguridad | API Key con variable `API_KEY_ENABLED` para apagarla en pruebas locales. |
| 26/10/2026 | Vigencia del enlace | Configurable en **horas** (`LINK_TTL_HOURS`), inicialmente **168** (1 semana). |
| 26/10/2026 | CI/CD | **No** incluido; despliegue **manual**. Los repositorios los crea el responsable. |
| 26/10/2026 | Reintentos de WhatsApp | Si el cliente no abre el enlace, **no** hay reenvíos (fuera de alcance). |
| 26/10/2026 | Normalización | Teléfono `XXXXXXXXX` (9 dígitos, sin prefijo de país). Nombres en MAYÚSCULAS, sin espacios dobles y conservando las tildes. |
| 26/10/2026 | Documentos de identidad | `DNI`, `RUC`, `CE`, `PASAPORTE`, `OTROS`. |
| 26/10/2026 | Información adicional | Campo JSON libre, hoy sin campos definidos. |
| 26/10/2026 | Página de agradecimiento | Diseño y texto: propuesta del responsable. |
| 26/10/2026 | PENDIENTE-02 · Teléfono tras el rechazo | El **teléfono se conserva** por trazabilidad. |
| 26/10/2026 | PENDIENTE-04 · Nueva transacción con registro activo | ~~Solo se **actualizan los datos** del registro existente (RN-04) y a KIZUNA se le envía la respuesta con el **ID de transacción anterior**.~~ **Reemplazada el 27/10/2026** (ver "RN-04 con un ID de transacción nuevo"). |
| 26/10/2026 | PENDIENTE-08 · Hosting de la landing | **Firebase Hosting**. |
| 26/10/2026 | PENDIENTE-17 · Nota del mockup | Prevalece RN-01: se **quita** la frase "* Autorización obligatoria para continuar". |
| 27/10/2026 | Health check | `GET /health` verifica también la base de datos y responde `{"status":"ok","database":"ok"}` (o `503` si la base no responde). |
| 27/10/2026 | Credenciales de la base de datos | Se elimina `DATABASE_PASSWORD`: la conexión usa solo `DATABASE_URL` con la **URL completa**. En el primer sprint se define en `.env` (desarrollo local); en Producción es un secreto de Secret Manager. |
| 27/10/2026 | `whatsapp_messages.to_phone` | Se amplía de `varchar(20)` a `varchar(30)`: el formato `whatsapp:+51XXXXXXXXX` ocupa 21 caracteres. |
| 27/10/2026 | Documentos alfanuméricos | En CE, PASAPORTE y OTROS, *alfanumérico* incluye la letra **Ñ** (A–Z, Ñ, 0–9; sin tildes). |
| 27/10/2026 | Errores de validación | Todos los mensajes del `422` se devuelven en español (también los de Pydantic) y sin el valor recibido. |
| 27/10/2026 | API Key sin configurar | Con `API_KEY_ENABLED=true` y `API_KEY` vacía, la aplicación **no arranca** (excepción al iniciar) y el despliegue falla. |
| 27/10/2026 | Respuestas 401 | Mensajes distintos: `"Falta la API Key"` (sin header) y `"API Key inválida"` (clave incorrecta). |
| 27/10/2026 | Comparación de la API Key | Exacta: no se recortan espacios ni se ignoran mayúsculas. |
| 27/10/2026 | Bloqueo en `POST /consents` | *Advisory locks* de PostgreSQL por `transaction_id` y por documento + Dealer (un bloqueo de fila no cubre el caso en que el registro aún no existe). |
| 27/10/2026 | `additional_info` en RN-04 | Si no se envía, se conserva el valor anterior; si se envía (incluso `null`), se reemplaza. |
| 27/10/2026 | Reenvío con otro documento o Dealer | Se conservan el documento y el Dealer del registro; solo se actualizan los datos del cliente y se registra un `WARNING`. |
| 27/10/2026 | Respuesta `409` | `{"detail", "transaction_id", "status", "is_deleted"}`. |
| 27/10/2026 | WhatsApp dentro de la transacción | El envío ocurre antes del commit, para que un reintento de KIZUNA tras un fallo genere un enlace nuevo. |
| 27/10/2026 | RN-04 con un ID de transacción nuevo | **Cambia la regla anterior.** El ID nuevo se registra como **copia vinculada** (`consents.source_transaction_id`) con las respuestas del registro principal, para que KIZUNA lo pueda consultar. Si el cliente aún no responde, la copia espera y recibe la respuesta del principal (M4). KIZUNA recibe una respuesta final por cada ID (M5). El índice único parcial pasa a `WHERE is_deleted = 0 AND source_transaction_id IS NULL` (migración `73cfe5a5377e`). |
| 27/10/2026 | Consultas (2.4) | Sin paginación. Fechas ISO 8601 UTC con `Z` y microsegundos. Lista vacía → `200 []`. |
| 27/10/2026 | Reenvío del ID de una copia vinculada | Solo se actualizan los datos del cliente de esa copia, no los del principal (paso 2 de la sección 2.3). |
| 27/10/2026 | `source_transaction_id` en las consultas | No se expone: la respuesta de la sección 2.4 es la misma para principales y copias. |
| 27/10/2026 | Copia vinculada que nace con respuesta | **También recibe su respuesta final a KIZUNA** (M5), aunque el cliente no vuelva a responder: KIZUNA recibe una respuesta por cada ID de transacción. |
| 27/10/2026 | Interruptor `TWILIO_SEND_ENABLED` | Por defecto `false`: en desarrollo no se envían WhatsApp reales. |
| 27/10/2026 | Configuración de Twilio incompleta | Con `TWILIO_SEND_ENABLED=true` y datos de Twilio faltantes o mal formados, la aplicación **no arranca** y el despliegue falla (mismo criterio que la API Key). |
| 27/10/2026 | `TWILIO_TIMEOUT_SECONDS` | Nueva variable (por defecto 10 s): un Twilio colgado no debe retener el bloqueo del cliente indefinidamente. |
| 27/10/2026 | `TWILIO_STATUS_CALLBACK_URL` vacía | La aplicación arranca con un `WARNING`; el mensaje se envía sin `status_callback`. |
| 27/10/2026 | Estados de Twilio al crear el mensaje | `accepted`, `sending` y `scheduled` se guardan como `queued`. Con el envío apagado, el mensaje queda `queued` sin SID. |
| 27/10/2026 | Errores HTTP en español | 400 (cuerpo ilegible), 404, 405, 500 y demás errores del framework se devuelven en español y en JSON; el 500 sin detalles internos. Los errores de SQL no incluyen parámetros. |
| 27/10/2026 | Entorno local | `docker compose up` levanta PostgreSQL y el mock de KIZUNA; la API corre en el host con `uv`. El perfil `api` del compose permite levantarla también en contenedor. |
| 27/10/2026 | Webhook de Twilio · orden de los estados | El estado de `whatsapp_messages` no retrocede (`queued` → `sent` → `delivered`/`failed`/`undelivered` → `read`); todos los callbacks se guardan como eventos. |
| 27/10/2026 | Webhook de Twilio · idempotencia | Un evento por mensaje y estado: UNIQUE (`message_id`, `status`) en `whatsapp_message_events` (migración `a43dda5e709f`). |
| 27/10/2026 | Webhook de Twilio · URL de la firma | Se valida contra `TWILIO_STATUS_CALLBACK_URL` y, como alternativa, contra la URL de la petición (detrás de Cloud Run la aplicación ve `http` interno). |
| 27/10/2026 | Webhook de Twilio · callbacks ignorados | `MessageSid` desconocido o estado no contemplado → `204` con `WARNING`. Sin `MessageSid`/`MessageStatus` → `422`. `accepted`/`sending`/`scheduled` se registran como `queued`. |
| 27/10/2026 | Webhook de Twilio · respuestas 403 | `"Falta la firma de Twilio"` (sin header) y `"Firma de Twilio inválida"`. |
| 27/10/2026 | Validación de firma apagada en Producción | Por ahora **no impide el arranque**: con `TWILIO_SIGNATURE_VALIDATION_ENABLED=false`, o encendida con `TWILIO_AUTH_TOKEN` vacío, solo se registra un `WARNING`, también con `APP_ENV=production`. |
| 27/10/2026 | M4 · Bloqueo del enlace | `SELECT ... FOR UPDATE` sobre el enlace **antes** de validar si está vencido o usado; luego sobre el registro principal. |
| 27/10/2026 | M4 · Enlace usado y vencido | Se informa `USED`. |
| 27/10/2026 | M4 · Body del envío | Los tres checks son obligatorios y booleanos estrictos de JSON; campos extra rechazados (`422`). |
| 27/10/2026 | M4 · Respuestas 404 y 410 | `"El enlace no existe"`; `410 {"detail", "link_status"}` con `"El enlace venció"` o `"El enlace ya fue utilizado"`. |
| 27/10/2026 | M4 · Respuestas en la evidencia | `consent_evidence.answers` guarda exactamente lo que envió el cliente, también el marketing en un rechazo (RN-01 se aplica en `consents`). |
| 27/10/2026 | M4 · IP de la evidencia | Se mantiene la **primera** IP de `X-Forwarded-For` (la nube de despliegue aún podría cambiar). Sin el header, la IP de la conexión; IP inválida → `null`. |
| 27/10/2026 | M4 · Política de privacidad sin configurar | `GET` devuelve `privacy_policy_url: null` y la evidencia guarda `""` (PENDIENTE-14). |
| 27/10/2026 | M4 · Notificación a KIZUNA | M4 crea las filas `PENDIENTE` (payload de `build_kizuna_payload()`) y dispara `dispatch_notifications()` después del commit; el envío HTTP es M5. |
| 27/10/2026 | M5 · Esperas entre reintentos | Si hay más reintentos que valores en `KIZUNA_CALLBACK_BACKOFF_SECONDS` se repite el último; sin valores, no hay espera. `Retry-After` no se usa. |
| 27/10/2026 | M5 · Timeout | `KIZUNA_CALLBACK_TIMEOUT_SECONDS` limita el intento completo. |
| 27/10/2026 | M5 · Respuestas 3xx | No se siguen; `FALLIDO` sin reintentar. |
| 27/10/2026 | M5 · `last_error` | Guarda el cuerpo de la respuesta truncado a 500 caracteres (no va al log); `null` tras un envío exitoso. |
| 27/10/2026 | M5 · Configuración al arrancar | Con `KIZUNA_CALLBACK_ENABLED=true` y sin URL, o con el header de autenticación sin su valor, la aplicación no arranca (`KizunaConfigurationError`). Apagado → `WARNING`. |
| 27/10/2026 | M5 · Payload y URL | Se fijan al crear la notificación. Las notificaciones de un mismo envío se envían en paralelo. |
| 27/10/2026 | M5 · Reproceso al arrancar | Nueva variable `KIZUNA_CALLBACK_REPROCESS_AFTER_MINUTES` (10). Al arrancar con el envío encendido se envían **todas** las `PENDIENTE` sin reclamar (antiguas y recientes, también las acumuladas con el envío apagado) y las de reclamo vencido. |
| 27/10/2026 | M5 · Reclamo con SKIP LOCKED | Cada notificación se reclama con `SELECT ... FOR UPDATE SKIP LOCKED` (`kizuna_notifications.claimed_at`, migración `94fef8d63ecf`) para que dos instancias no la envíen dos veces. |
| 27/10/2026 | M5 · `transaction_id` en RN-04 | Se mantiene la regla del 27/10 ("RN-04 con un ID de transacción nuevo"): cada ID (principal y copias) se notifica con **su propio** `transaction_id`. |
| 27/10/2026 | M7 · Imagen | `Dockerfile` en dos etapas (uv solo en la de construcción), usuario sin privilegios, `uvicorn` en `$PORT` con `--proxy-headers --forwarded-allow-ips '*'` para ver `https` detrás de Cloud Run. |
| 27/10/2026 | M7 · Variables no secretas | Se pasan con `--env-vars-file deploy/cloudrun.env.yaml` en lugar de `--set-env-vars` (hay valores con comas). Plantilla inicial con `TWILIO_SEND_ENABLED=false`, `KIZUNA_CALLBACK_ENABLED=false` y `DOCS_ENABLED=true` (PENDIENTE-12). |
| 27/10/2026 | M7 · Usuarios de base de datos | `consent_owner` corre las migraciones; `consent_app` (creado por SQL, solo DML) es el de la aplicación, porque los usuarios creados con `gcloud` reciben `cloudsqlsuperuser`. |
| 27/10/2026 | M7 · Secretos | `KIZUNA_CALLBACK_AUTH_VALUE` queda fuera de `--set-secrets` hasta que PENDIENTE-13 defina la autenticación de KIZUNA. Los secretos se montan con `:latest`. |
| 27/10/2026 | M7 · Tier de Cloud SQL | `db-g1-small` requiere `--edition=ENTERPRISE` (Enterprise Plus no ofrece tiers de núcleo compartido); el tier sigue a validar con Yamaha. |
