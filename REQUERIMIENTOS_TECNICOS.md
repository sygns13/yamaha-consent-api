# Hoja de Requerimientos Técnicos — Gestión Digital de Conformidades (KIZUNA · WhatsApp · Landing)

| Campo | Valor |
|---|---|
| Cliente | Yamaha – Perú |
| Proveedor / Responsable técnico | Cristian Fernando Chávez Torres |
| Versión del documento | 1.2 — 26/10/2026 (incorpora las decisiones de la sección 11) |
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
| RN-04 | Si llega información de un cliente con un **registro activo** (`is_deleted = 0`) para el mismo Dealer, se **actualizan los datos del cliente** (nombre, teléfono, información adicional), **no** se crea un registro nuevo, **no** se reenvía el WhatsApp y las conformidades se mantienen. Aunque llegue con un **nuevo ID de transacción**, la respuesta a KIZUNA se envía con el **ID de transacción del registro existente (anterior)**. | Backend (M2) |
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

**Criterios de aceptación**

- `docker compose up` levanta PostgreSQL y el mock de KIZUNA, y la API responde `GET /health` → `200 {"status":"ok"}`.
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
| `created_at` / `updated_at` | timestamptz | no | |

- **Sin restricción única** sobre documento + Dealer: un mismo cliente puede tener varios registros con el mismo Dealer (RN-05).
- **Índices:** `(document_type, document_number, dealer, is_deleted)`, `status`, `created_at`.
- **Registro activo** de un documento + Dealer: el más reciente con `is_deleted = 0`. La aplicación debe garantizar que haya **como máximo uno** (bloqueo de fila o índice único parcial `WHERE is_deleted = 0`, **(propuesta)**).

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
| `answers` | jsonb | `{"personal_data":bool,"marketing_yamaha":bool,"marketing_dealer":bool}` |
| `submitted_at` | timestamptz | |

> **Decisión:** el borrado lógico **no** afecta esta tabla ni `whatsapp_messages`; solo se anula el nombre del cliente en `consents`.

### Tabla `whatsapp_messages`

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | uuid PK | |
| `transaction_id` | varchar(100) FK → consents | |
| `link_id` | uuid FK → consent_links | |
| `twilio_message_sid` | varchar(64) UNIQUE null | Nulo si falló antes de llegar a Twilio |
| `to_phone` | varchar(20) | `whatsapp:+51XXXXXXXXX` |
| `content_sid` | varchar(64) | Plantilla usada |
| `status` | whatsapp_status | Último estado conocido |
| `error_code` | varchar(20) null | Código de error de Twilio |
| `error_message` | text null | |
| `created_at` / `updated_at` | timestamptz | |

### Tabla `whatsapp_message_events` (registro de cada callback de Twilio)

`id`, `message_id` FK, `status`, `error_code`, `raw_payload` jsonb, `received_at`.

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

### Criterios de aceptación M1

- `alembic upgrade head` crea todo el esquema en una base vacía; `alembic downgrade base` lo revierte.
- `consents.transaction_id` es PK: insertar dos veces el mismo ID de transacción falla.
- Se pueden insertar dos registros con el mismo documento + Dealer si el anterior tiene `is_deleted = 1`.
- `is_deleted` rechaza valores distintos de 0 y 1.

---

## M2 — Backend core y API para KIZUNA

### 2.1 Configuración y seguridad

- La configuración se carga con `pydantic-settings` (sección 9).
- **API Key** para los endpoints de KIZUNA:
  - Header: `X-API-Key` (nombre configurable con `API_KEY_HEADER`).
  - `API_KEY_ENABLED=true|false`. Con `false` **no se valida** (pruebas locales o con Postman). Al arrancar, si está en `false` se emite un log `WARNING`.
  - Comparación en tiempo constante (`secrets.compare_digest`).
  - Sin clave o con clave inválida → `401`.
- Los endpoints públicos (`/api/v1/public/*`) **no** usan API Key: los protege el token del enlace.
- CORS: solo los orígenes de `CORS_ALLOWED_ORIGINS` (dominio de la landing).

### 2.2 Normalización y validación de entrada

| Campo | Normalización | Validación |
|---|---|---|
| `document_type` | Mayúsculas | Debe estar en el enum |
| `document_number` | Trim, quitar espacios y guiones, mayúsculas | DNI: 8 dígitos · RUC: 11 dígitos · CE: alfanumérico, máximo 12 · PASAPORTE: alfanumérico, máximo 12 · OTROS: alfanumérico, máximo 20 **(propuesta)** |
| `full_name` | Trim, colapsar espacios múltiples, **MAYÚSCULAS conservando tildes y Ñ** (`str.upper()`) | No vacío, máximo 250 |
| `phone` | Quitar todo lo que no sea dígito; si quedan 11 dígitos y empieza con `51`, quitar `51` | Exactamente **9 dígitos** (formato `XXXXXXXXX`) |
| `dealer` | Trim | No vacío (⚠️ PENDIENTE-05) |
| `additional_info` | — | Objeto JSON opcional |

> Al enviar por Twilio, el teléfono se arma como `whatsapp:+51` + `phone`. ⚠️ PENDIENTE-10: los números extranjeros no son compatibles con la regla de 9 dígitos.

Un error de validación responde `422` con el detalle del campo.

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
1. Validar y normalizar.
2. **El `transaction_id` ya existe** (reenvío de la misma transacción) → **RN-04**: actualizar `full_name`, `phone` y `additional_info`, y registrar `consent_requests` (`UPDATED`). Sin enlace ni WhatsApp nuevos. Responder `200`.
   - Si ese registro tiene `is_deleted = 1` → responder `409` con el estado actual, sin modificar nada **(propuesta)**.
3. Buscar el **registro activo** del documento + Dealer (`is_deleted = 0`, el más reciente).
4. **Hay registro activo** (pendiente o aceptado) → **RN-04**: actualizar los datos del cliente en ese registro y registrar `consent_requests` (`UPDATED`, con el nuevo `transaction_id` y la referencia al registro afectado). **No** se crea registro, **no** se reenvía WhatsApp y **no** se tocan las conformidades. Responder `200`. El nuevo ID de transacción **no** se convierte en registro: la respuesta final a KIZUNA (M5) se envía con el **ID de transacción del registro existente (anterior)**, que se devuelve en `affected_transaction_id`.
5. **No hay registro activo** (nunca existió, o solo hay registros con `is_deleted = 1` → **RN-05**) → crear `consents` con el `transaction_id` recibido (`PENDIENTE`, `is_deleted = 0`), registrar `consent_requests` (`CREATED`), crear `consent_links` con token nuevo y **enviar el WhatsApp (M3)**. Responder `201`.
6. Si falla el envío por Twilio, el registro **se conserva**, el error queda en `whatsapp_messages` y la respuesta lo indica (no es un error 5xx).

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
- `affected_transaction_id`: el registro que se creó o actualizó; en el paso 4 es el ID del registro activo previo.
- Si `result` es `UPDATED`, `whatsapp` vale `null`.

### 2.4 Endpoints de consulta (requieren API Key)

- `GET /api/v1/consents/{transaction_id}` → un registro (`404` si no existe).
- `GET /api/v1/consents?document_type=DNI&document_number=12345678&dealer=DEALER-001` → **lista** de registros de ese documento (y Dealer, opcional), ordenada por `created_at` descendente. Incluye los borrados lógicamente.

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
- Envío mediante **Content API**: `messages.create(from_=TWILIO_WHATSAPP_FROM, to="whatsapp:+51...", content_sid=TWILIO_CONTENT_SID, content_variables=json.dumps({...}), status_callback=TWILIO_STATUS_CALLBACK_URL)`.
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

- Recibe los callbacks de estado de Twilio (`application/x-www-form-urlencoded`): `MessageSid`, `MessageStatus`, `ErrorCode`, `ErrorMessage`.
- **Valida la firma** `X-Twilio-Signature` con `twilio.request_validator.RequestValidator` y `TWILIO_AUTH_TOKEN`. Firma inválida → `403`. La validación se puede apagar con `TWILIO_SIGNATURE_VALIDATION_ENABLED=false` (solo para local, **(propuesta)**).
- Guarda el evento en `whatsapp_message_events` y actualiza `whatsapp_messages.status`, `error_code` y `error_message`.
- Es idempotente ante callbacks repetidos y responde `204` rápido.
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
- `link_status`: `VALID` | `EXPIRED` (`now > expires_at`) | `USED` (`used_at` no nulo).
- Token inexistente → `404` (la landing lo trata igual que un enlace inválido).
- ⚠️ PENDIENTE-05: ¿se muestra el nombre del Dealer en la landing? El mockup dice "DEALER" genérico. Mientras tanto `dealer_display_name` vale `null` y la landing muestra "DEALER".
- **No** devuelve datos personales.

### `POST /api/v1/public/consent-links/{token}/submit` — registrar respuestas

**Request**
```json
{ "personal_data": true, "marketing_yamaha": false, "marketing_dealer": true }
```

**Lógica (en una sola transacción de base de datos)**
1. Validar el enlace: si es `EXPIRED` o `USED` → `410 Gone` con `link_status`.
2. Bloquear la fila (`SELECT ... FOR UPDATE`) para evitar doble envío.
3. Registrar `consent_evidence` (IP, user agent, versión y URL de la política, respuestas, fecha).
4. Si `personal_data = true` → `status = ACEPTADO` y se guardan los 3 valores.
5. Si `personal_data = false` → **RN-01** (prevalece; se acepta el envío con todos los checks desmarcados):
   - `status = NO_ACEPTADO`, `consent_personal_data = false`.
   - **Borrado lógico:** `full_name = null`, `is_deleted = 1`, `deleted_at = now()`.
   - `consent_marketing_yamaha = null` y `consent_marketing_dealer = null` (RN-01: no se conservan las respuestas de marketing).
   - Se conservan: `transaction_id`, `document_type`, `document_number`, `dealer`, `phone` (por trazabilidad), `additional_info`, `consent_evidence` y `whatsapp_messages`.
6. `responded_at = now()` y `consent_links.used_at = now()`.
7. Crear el registro en `kizuna_notifications` (`PENDIENTE`) y disparar el envío (M5) **después del commit**.
8. Responder `200 {"status":"ACEPTADO"|"NO_ACEPTADO"}`. La landing redirige a `/gracias` en ambos casos.

### Criterios de aceptación M4

- Tests: aceptación completa; aceptación con marketing en `false`; RN-01 con los tres checks desmarcados (verificar `full_name = null`, `marketing_* = null`, `is_deleted = 1` y documento, Dealer y transacción conservados); enlace vencido (410); enlace usado (410); doble envío concurrente (solo uno se registra).

---

## M5 — Notificación a KIZUNA (outbox y reintentos)

### Requerimientos

- Tras el envío del cliente (M4), el backend hace `POST` a `KIZUNA_CALLBACK_URL` con la respuesta final.
- **Interruptor:** `KIZUNA_CALLBACK_ENABLED=true|false`. Con `false`, **no se dispara ninguna petición**: la notificación se guarda en `kizuna_notifications` con estado `PENDIENTE` y se registra en el log que el envío está desactivado. Por defecto en `.env.example`: `false`.
- **Mientras no exista el endpoint real de KIZUNA**, el envío apunta a un **mock** (ver más abajo). Pasar al endpoint real debe requerir **cambios mínimos**: la URL y la autenticación por variables de entorno, y los campos en **una sola función** de mapeo (`build_kizuna_payload()` en `integrations/kizuna_client.py`).
- **Reintentos:** 1 intento inicial + **hasta 3 reintentos** (`KIZUNA_CALLBACK_MAX_RETRIES=3`), con espera configurable entre intentos (`KIZUNA_CALLBACK_BACKOFF_SECONDS`, por ejemplo `5,30,120`) y timeout por intento (`KIZUNA_CALLBACK_TIMEOUT_SECONDS`).
- Se reintenta ante error de red, timeout o respuesta `5xx`/`429`. Ante otros `4xx` se marca `FALLIDO` sin reintentar **(propuesta)**.
- Cada intento actualiza `kizuna_notifications` (`attempts`, `last_http_status`, `last_error`). Si se agotan los reintentos → `FALLIDO` y log `ERROR`. El endpoint de consulta (M2) sigue siendo la vía de respaldo.
- Ejecución: `BackgroundTasks` de FastAPI después del commit. En Cloud Run se requiere **CPU siempre asignada** (M7) para que la tarea no se congele. *(Alternativa si se requiere más robustez: Cloud Tasks.)*
- Al arrancar la app, las notificaciones `PENDIENTE` con más de N minutos sin intento se reprocesan **(propuesta)**.

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
- Valor local: `KIZUNA_CALLBACK_URL=http://kizuna-mock:8081/kizuna/consent-result`.
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
| Cloud SQL | PostgreSQL 16; tier pequeño (por ejemplo `db-g1-small` o equivalente, a validar con Yamaha); backups automáticos activados; base `consents_db` y usuario de aplicación con privilegios mínimos |
| Secret Manager | `DATABASE_PASSWORD`, `API_KEY`, `TWILIO_AUTH_TOKEN`, `KIZUNA_CALLBACK_AUTH_VALUE` |
| Artifact Registry | Repositorio Docker `consent-api` |
| Service Account | `consent-api-sa` con roles `Cloud SQL Client` y `Secret Manager Secret Accessor` |
| Cloud Run `consent-api` | `--min-instances=1`, `--no-cpu-throttling` (CPU siempre asignada, por M5), `--add-cloudsql-instances`, `--set-secrets`, ingreso público (lo usan KIZUNA y Twilio) |
| Firebase Hosting | Se agrega Firebase al **mismo proyecto de Google Cloud**; sitio `consent-landing`; dominio propio verificado con registros DNS en Hostinger; SSL automático |

### Pasos de despliegue manual (documentar en el `README.md` de cada repositorio)

1. `docker build` y `docker push` de la imagen a Artifact Registry (o `gcloud builds submit`).
2. Ejecutar migraciones: Cloud SQL Auth Proxy local + `alembic upgrade head`.
3. `gcloud run deploy consent-api --image ... --region <REGION> --service-account consent-api-sa --min-instances=1 --no-cpu-throttling --add-cloudsql-instances <INSTANCE> --set-env-vars ... --set-secrets ...`
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
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/consents_db` | En Cloud Run, vía socket de Cloud SQL |
| `API_KEY_ENABLED` | `true` | `false` desactiva la validación (pruebas locales) |
| `API_KEY_HEADER` | `X-API-Key` | |
| `API_KEY` | *(secreto)* | |
| `CORS_ALLOWED_ORIGINS` | `https://consentimiento.dominio.com` | Lista separada por comas |
| `LINK_TTL_HOURS` | `168` | Vigencia del enlace en **horas** (1 semana) |
| `LANDING_BASE_URL` | `https://consentimiento.dominio.com` | Base del enlace enviado por WhatsApp |
| `PRIVACY_POLICY_URL` | ⚠️ PENDIENTE-14 | |
| `PRIVACY_POLICY_VERSION` | `v1` | Se guarda en la evidencia |
| `TWILIO_ACCOUNT_SID` | | |
| `TWILIO_AUTH_TOKEN` | *(secreto)* | |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+1XXXXXXXXXX` | Número nuevo y exclusivo |
| `TWILIO_CONTENT_SID` | `HXxxxxxxxx` | Plantilla Call to Action aprobada (botón "Revisar autorizaciones") |
| `TWILIO_STATUS_CALLBACK_URL` | `https://<api>/api/v1/webhooks/twilio/status` | |
| `TWILIO_SIGNATURE_VALIDATION_ENABLED` | `true` | (propuesta) |
| `PHONE_COUNTRY_PREFIX` | `51` | Se antepone al enviar |
| `KIZUNA_CALLBACK_ENABLED` | `false` | Enciende o apaga el envío final a KIZUNA. Con `false` no sale ninguna petición |
| `KIZUNA_CALLBACK_URL` | `http://kizuna-mock:8081/kizuna/consent-result` (local) | Endpoint real: ⚠️ PENDIENTE-13 |
| `KIZUNA_CALLBACK_AUTH_HEADER` | ⚠️ PENDIENTE-13 | |
| `KIZUNA_CALLBACK_AUTH_VALUE` | *(secreto)* | |
| `KIZUNA_CALLBACK_MAX_RETRIES` | `3` | Reintentos después del primer intento |
| `KIZUNA_CALLBACK_BACKOFF_SECONDS` | `5,30,120` | Espera antes de cada reintento |
| `KIZUNA_CALLBACK_TIMEOUT_SECONDS` | `10` | |

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
| 26/10/2026 | PENDIENTE-04 · Nueva transacción con registro activo | Solo se **actualizan los datos** del registro existente (RN-04) y a KIZUNA se le envía la respuesta con el **ID de transacción anterior**. |
| 26/10/2026 | PENDIENTE-08 · Hosting de la landing | **Firebase Hosting**. |
| 26/10/2026 | PENDIENTE-17 · Nota del mockup | Prevalece RN-01: se **quita** la frase "* Autorización obligatoria para continuar". |
