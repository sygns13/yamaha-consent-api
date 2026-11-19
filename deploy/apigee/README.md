# Apigee: OAuth 2.0 para KIZUNA

Fuente de verdad: `../../../requerimientos/INTEGRACION_KIZUNA_OAUTH2.spec.md` (secciones 3, 7 y 9).

KIZUNA pide un token a Apigee con client credentials y llama a `/api/v1/consents` con `Authorization: Bearer`. Apigee
valida el token y reenvía a la API con su API Key (`X-API-Key`), que KIZUNA no conoce.

| Carpeta | Proxy | Qué hace |
|---|---|---|
| `kizuna-oauth/apiproxy/` | `kizuna-oauth`, basepath `/oauth2` | `POST /oauth2/token`: emite el token (`OAuthV2 GenerateAccessToken`, 3600 s), sin backend |
| `kizuna-consents/apiproxy/` | `kizuna-consents`, basepath `/api/v1/consents` | Valida el Bearer (scope `kizuna.consents`), pone `X-API-Key` desde el KVM y reenvía al TargetServer `consent-api`. Otras rutas → `404` |

Los bundles no tienen hosts, URLs ni secretos: el destino es el **TargetServer** `consent-api` y la API Key está en el
**KVM** `consent-backend` del entorno. El mismo bundle sirve para la organización de evaluación (destino: VPS de
pruebas) y para Producción (destino: Cloud Run). `tests/test_apigee_bundles.py` verifica el XML sin red.

Los comandos usan la API REST de Apigee (`https://apigee.googleapis.com/v1`) con `curl` y el token de `gcloud`; corren
en Git Bash, Linux o macOS. Los secretos se leen con `read -rs` y se pasan por la entrada estándar: nunca quedan en el
historial ni en archivos.

## 1. Empaquetar

```bash
cd yamaha-consent-api
uv run python -m scripts.package_apigee      # → deploy/apigee/dist/kizuna-oauth.zip y kizuna-consents.zip
```

`dist/` está en `.gitignore`. Cada zip tiene `apiproxy/` en la raíz, que es lo que espera la importación.

## 2. Organización de evaluación (criterio K2.3, en tu proyecto de Google Cloud)

Sirve solo para probar los bundles antes de Producción (PENDIENTE-29, resuelto el 18/11/2026). Apunta al VPS de
pruebas; KIZUNA no la usa. **Paso a paso desde cero** (proyecto, asistente, DNS en Hostinger o nip.io, pruebas y
limpieza): `../../../GUIA_APIGEE_PRUEBAS_HOSTINGER.md`.

### 2.1. Requisitos

- Un proyecto de Google Cloud **tuyo**, con facturación activa. La organización de evaluación es gratis por 60 días,
  pero el balanceador, la IP estática y Compute Engine que crea el asistente se cobran (poco) mientras existan.
- Rol **Apigee Organization Admin** (u Owner del proyecto).
- La red `default` con un bloque `/22` libre (el asistente lo reserva solo).
- En tu PC: `gcloud` con sesión iniciada en ese proyecto, `curl`, `python` y `uv`.
- La API Key del VPS: `API_KEY` de `/var/www/consentimientos-test/.env.api`. Léela por SSH sin copiarla a archivos.

### 2.2. Crear la organización (consola)

Google Cloud Console → **Apigee** → evaluación gratuita. El asistente pide:

1. **Habilitar APIs:** Apigee, Compute Engine y Service Networking.
2. **Redes:** red `default` y rango IP asignado automáticamente (`/22`).
3. **Hosting:** región de análisis y ubicación del runtime (la más cercana; no tiene que ser la de Producción).
4. **Acceso:** **External** con el servicio **nip.io** (no hace falta dominio propio). El asistente reserva una IP
   estática y crea el balanceador.

Tarda hasta 45 minutos. Crea:
- la organización (mismo nombre que el proyecto);
- la instancia `eval-instance`;
- el entorno **`eval`**;
- el grupo de entornos **`eval-group`**, con su hostname `nip.io`;
- un proxy de ejemplo `hello-world`.

Vence a los 60 días (aviso al día 30) y no se puede pasar a una organización paga.

### 2.3. Variables

```bash
export ORG=<PROJECT_ID>                      # la organización de evaluación se llama como el proyecto
export ENV=eval
export APIGEE_HOST=<hostname nip.io>         # Apigee → Management → Environments → Environment groups → eval-group
export BACKEND_HOST=<host de la API en el VPS de pruebas>   # el de GUIA_DESPLIEGUE_VPS_PRUEBAS.md, sin https://
export DEV_EMAIL=<tu correo>                 # dueño del Developer de prueba
export APP_NAME=kizuna-eval
```

Después sigue la sección 3 (recursos) y la 4 (lista de verificación).

## 3. Recursos en Apigee (evaluación y Producción)

Con las variables de 2.3 (evaluación) o de la guía de Producción, sección 20 (`ENV=prod`, `BACKEND_HOST` = host de
Cloud Run, `APP_NAME=kizuna-prod`).

```bash
TOKEN=$(gcloud auth print-access-token)
API=https://apigee.googleapis.com/v1/organizations/$ORG
```

El token de `gcloud` dura una hora: si un comando responde `401`, vuelve a ejecutar la línea `TOKEN=...`.

### 3.1. TargetServer `consent-api` (destino con TLS)

```bash
curl -sS -X POST "$API/environments/$ENV/targetservers" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\": \"consent-api\", \"host\": \"$BACKEND_HOST\", \"port\": 443, \"isEnabled\": true, \"sSLInfo\": {\"enabled\": true}}"
```

### 3.2. KVM cifrado `consent-backend` con la API Key

En Apigee X todos los KVM son cifrados. La key se lee sin eco y viaja por la entrada estándar:

```bash
curl -sS -X POST "$API/environments/$ENV/keyvaluemaps" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "consent-backend", "encrypted": true}'

read -rs BACKEND_KEY                          # Producción: el valor del secreto API_KEY; evaluación: el del VPS
printf '{"name": "api_key", "value": "%s"}' "$BACKEND_KEY" | curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  "$API/environments/$ENV/keyvaluemaps/consent-backend/entries" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data-binary @-
unset BACKEND_KEY
```

La respuesta de la entrada trae el valor: por eso se descarta con `-o /dev/null` y solo se muestra el código HTTP
(`201`). Para cambiar la key: `DELETE .../entries/api_key` y volver a crearla.

### 3.3. Importar y desplegar los proxies

```bash
uv run python -m scripts.package_apigee
for PROXY in kizuna-oauth kizuna-consents; do
  REV=$(curl -sS -X POST "$API/apis?name=$PROXY&action=import" \
    -H "Authorization: Bearer $TOKEN" -F "file=@deploy/apigee/dist/$PROXY.zip" \
    | python -c "import json, sys; print(json.load(sys.stdin)['revision'])")
  echo "$PROXY: revisión $REV"
  curl -sS -X POST "$API/environments/$ENV/apis/$PROXY/revisions/$REV/deployments?override=true" \
    -H "Authorization: Bearer $TOKEN"
done
```

- Importar un proxy que ya existe crea una **revisión nueva**. `override=true` reemplaza la desplegada sin corte.
- Si la importación falla por un error de validación del XML, la respuesta lo indica (política y elemento).
- Alternativa desde la consola: Apigee → Proxy development → API proxies → el proxy → **Deploy** (elegir revisión y
  entorno).

### 3.4. API Product, Developer y App

El Product incluye **todo** el proxy (`/**`): las rutas las restringe el proxy, con un `404`.

```bash
curl -sS -X POST "$API/apiproducts" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d @- <<JSON
{
  "name": "kizuna-consents",
  "displayName": "KIZUNA consentimientos",
  "approvalType": "auto",
  "environments": ["$ENV"],
  "scopes": ["kizuna.consents"],
  "attributes": [{"name": "access", "value": "private"}],
  "operationGroup": {"operationConfigs": [{"apiSource": "kizuna-consents", "operations": [{"resource": "/**"}]}]}
}
JSON

curl -sS -X POST "$API/developers" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"email\": \"$DEV_EMAIL\", \"firstName\": \"KIZUNA\", \"lastName\": \"Integracion\", \"userName\": \"kizuna\"}"

curl -sS -X POST "$API/developers/$DEV_EMAIL/apps" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d "{\"name\": \"$APP_NAME\", \"apiProducts\": [\"kizuna-consents\"]}" \
  | python -c "import json, sys; c = json.load(sys.stdin)['credentials'][0]; print('client_id     =', c['consumerKey']); print('client_secret =', c['consumerSecret'])"
```

- `client_id` = Consumer Key y `client_secret` = Consumer Secret. Salen **una sola vez** por la terminal. En
  Producción se entregan a KIZUNA por un canal seguro (el `client_secret` en un mensaje distinto del `client_id`) y no
  se guardan en archivos.
- Sin `keyExpiresIn`, la credencial no vence (decisión del spec, 3.4).
- Para verlas de nuevo: Apigee → Distribution → Apps → la App → Credentials.

### 3.5. Rotar la credencial de KIZUNA (sin corte)

1. Consola: Apigee → Distribution → Apps → la App → **Add credential** (o "Add key"), asociada al product
   `kizuna-consents`. Las dos credenciales sirven a la vez.
2. Entrega la nueva a KIZUNA. Cuando confirme el cambio, **revoca** la anterior en la misma pantalla.

### 3.6. Publicar un cambio en un proxy

Edita el XML, corre `uv run pytest tests/test_apigee_bundles.py` y repite 3.3: se crea y despliega una revisión nueva.

## 4. Lista de verificación (K2.3; en Producción, K3)

```bash
BASE=https://$APIGEE_HOST
read -r CLIENT_ID; read -rs CLIENT_SECRET   # los de 3.4
TOK=$(curl -sS -X POST "$BASE/oauth2/token" -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode grant_type=client_credentials --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "client_secret=$CLIENT_SECRET" --data-urlencode scope=kizuna.consents \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
```

Anota cada resultado en `REPORTE_CRITERIOS_KIZUNA_OAUTH2.md`. Lo marcado ⚠ no está confirmado por la documentación de
Apigee: si el resultado difiere, ajusta el bundle (y su test) y anótalo.

| # | Prueba | Esperado |
|---|---|---|
| 1 | Token con credenciales en el cuerpo (comando de arriba, con `-i`) | `200`; `{"access_token", "token_type": "Bearer", "expires_in": 3600 o 3599, "scope": "kizuna.consents"}`; `Cache-Control: no-store`; sin correo del Developer |
| 2 | Token con Basic: `curl -i -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials $BASE/oauth2/token` | `200` igual que 1 |
| 3 | Secreto equivocado | `401 {"error": "invalid_client", ...}` con `WWW-Authenticate: Basic ...` |
| 4 | Sin `grant_type`; `grant_type=password`; cuerpo JSON | `400 invalid_request`; `400 unsupported_grant_type`; `400 invalid_request` |
| 5 | ⚠ Sin `scope` | `200`. Anota el `scope` del token: debe permitir la prueba 7. Si no, KIZUNA tendrá que enviar `scope=kizuna.consents` (avisar) |
| 6 | ⚠ `scope=otro` | Anota código y cuerpo. Esperado: `400 invalid_scope`; si Apigee da otro fallo, agrega su `FaultRule` y corrige el Anexo A |
| 7 | `POST $BASE/api/v1/consents` con `Authorization: Bearer $TOK` y un cliente de prueba (`dealerCode` `DEALER-TEST`) | `201` de la API. ⚠ Confirma que Apigee envía al VPS el Host y el SNI correctos (si no, Traefik responde 404 o falla TLS) |
| 8 | `GET $BASE/api/v1/consents?customerId=...&dealerCode=DEALER-TEST` y `GET $BASE/api/v1/consents/<IdentifierId>` | `200` (los mismos datos que directo al VPS) |
| 9 | Sin `Authorization`; `Bearer` inventado; `Authorization: Basic abc` | `401 {"detail": "Falta el token de acceso"}`; `401 {"detail": "Token de acceso inválido o vencido"}` (las dos últimas) |
| 10 | Con token: `DELETE $BASE/api/v1/consents/x` y `GET $BASE/api/v1/consents/a/b` | `404 {"detail": "Recurso no encontrado"}`. (Una ruta sin proxy, como `$BASE/api/v1/otra`, da el 404 propio de Apigee: no pasa por estos bundles) |
| 11 | ⚠ Con token: `POST $BASE/api/v1/consents/` y `GET $BASE/api/v1/consents/` (con barra final) | `404 {"detail": "Recurso no encontrado"}` de Apigee. Sin barra (7 y 8) debe funcionar: valida las condiciones `proxy.pathsuffix = null` / `""`. La barra final se bloquea porque la API respondería `307` con un `Location` hacia el host del backend |
| 12 | Revocar la credencial en la consola y repetir 1 y 7 | Token: `401 invalid_client`; consumo con el token anterior: `401` |
| 13 | Llamar directo al VPS sin `X-API-Key` | `401` de la API (Apigee no es la única protección) |

Al terminar:
- borra los datos de prueba del VPS con el procedimiento de su guía;
- si no vas a seguir usando la organización, elimina el balanceador, la IP estática y la NEG que creó el asistente
  (VPC network → IP addresses; Network services → Load balancing), para no pagar hasta el día 60.
