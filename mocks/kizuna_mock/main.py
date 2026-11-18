"""Mock de KIZUNA para pruebas locales (M5). No se despliega en Producción.

Es independiente del backend (no importa `app`): corre en su propio contenedor.

Variables de entorno (se leen en cada petición):
- MOCK_FAIL_TIMES=N: responde 500 a las primeras N peticiones a /kizuna/consent-result.
- MOCK_RESPONSE_DELAY_SECONDS=S: espera S segundos antes de responder (simula un timeout).

OAuth 2.0 (INTEGRACION_KIZUNA_OAUTH2.spec.md, sección 6). POST /kizuna/oauth/token imita al Entra ID de KIZUNA
(client credentials):
- MOCK_REQUIRE_AUTH=true: /kizuna/consent-result exige un Bearer vigente emitido por el mock (si no, 401).
- MOCK_CLIENT_ID / MOCK_CLIENT_SECRET: credenciales válidas (por defecto mock-client / mock-secret).
- MOCK_TOKEN_EXPIRES_IN: expires_in de la respuesta (por defecto 3600).
- MOCK_TOKEN_FAIL_TIMES=N: responde 500 a los primeros N pedidos de token.
Nunca registra en el log el secreto ni los tokens.
"""

import asyncio
import base64
import binascii
import logging
import os
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("kizuna_mock")


class ConsentResult(BaseModel):
    """Contrato que definió KIZUNA para la respuesta final. Los checks son booleanos, nunca null."""

    IdentifierId: str
    customerId: str
    dealerCode: str
    consentDateTime: datetime
    personalDataConsent: bool
    marketingConsentYMDP: bool
    marketingConsentDEALER: bool


class MockState:
    def __init__(self) -> None:
        self.total_requests = 0
        self.received: list[dict[str, Any]] = []
        self.token_requests = 0
        # Tokens emitidos y vigentes (DELETE /kizuna/oauth/tokens los invalida).
        self.tokens: set[str] = set()


state = MockState()
app = FastAPI(title="KIZUNA mock", version="0.2.0")


def mask_document(value: str) -> str:
    """Mismo criterio que app/utils/masking.py: solo los últimos 4 caracteres visibles."""
    return "*" * len(value) if len(value) <= 4 else "*" * (len(value) - 4) + value[-4:]


def env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


@app.post("/kizuna/consent-result")
async def consent_result(payload: ConsentResult, request: Request) -> JSONResponse:
    state.total_requests += 1
    attempt = state.total_requests

    authenticated = bearer_token(request) in state.tokens
    if env_flag("MOCK_REQUIRE_AUTH") and not authenticated:
        logger.warning("Callback sin un Bearer vigente para IdentifierId=%s", payload.IdentifierId)
        return JSONResponse({"error": "invalid_token"}, status_code=401)

    delay = float(os.getenv("MOCK_RESPONSE_DELAY_SECONDS", "0"))
    if delay > 0:
        await asyncio.sleep(delay)

    fail_times = int(os.getenv("MOCK_FAIL_TIMES", "0"))
    if attempt <= fail_times:
        logger.warning("Fallo simulado %s/%s para IdentifierId=%s", attempt, fail_times, payload.IdentifierId)
        return JSONResponse({"error": "fallo simulado"}, status_code=500)

    state.received.append(
        {
            "received_at": datetime.now(UTC).isoformat(),
            "authenticated": authenticated,
            "payload": payload.model_dump(mode="json"),
        }
    )
    logger.info(
        "Recibido IdentifierId=%s customerId=%s personalDataConsent=%s",
        payload.IdentifierId,
        mask_document(payload.customerId),
        payload.personalDataConsent,
    )
    return JSONResponse({"received": True})


def oauth_error(status_code: int, error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status_code)


def client_credentials(request: Request, form: dict[str, str]) -> tuple[str, str] | None:
    """Credenciales en el cuerpo (client_secret_post) o en Basic (client_secret_basic, RFC 6749 §2.3.1)."""
    scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        client_id, separator, secret = decoded.partition(":")
        return (unquote_plus(client_id), unquote_plus(secret)) if separator else None
    if "client_id" in form and "client_secret" in form:
        return form["client_id"], form["client_secret"]
    return None


def valid_credentials(credentials: tuple[str, str] | None) -> bool:
    if credentials is None:
        return False
    expected = (os.getenv("MOCK_CLIENT_ID", "mock-client"), os.getenv("MOCK_CLIENT_SECRET", "mock-secret"))
    return all(
        secrets.compare_digest(given.encode("utf-8"), wanted.encode("utf-8"))
        for given, wanted in zip(credentials, expected, strict=True)
    )


@app.post("/kizuna/oauth/token")
async def token(request: Request) -> JSONResponse:
    """Client credentials, con las respuestas de Entra ID. El token es opaco para quien lo recibe."""
    state.token_requests += 1
    fail_times = int(os.getenv("MOCK_TOKEN_FAIL_TIMES", "0"))
    if state.token_requests <= fail_times:
        logger.warning("Fallo simulado del token %s/%s", state.token_requests, fail_times)
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=500)

    if not request.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
        return oauth_error(400, "invalid_request", "El cuerpo debe ser application/x-www-form-urlencoded")
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    form = {key: values[0] for key, values in parsed.items()}

    has_basic = request.headers.get("Authorization", "").lower().startswith("basic ")
    if has_basic and ("client_id" in form or "client_secret" in form):
        return oauth_error(400, "invalid_request", "Use un solo método de autenticación del cliente")
    if "grant_type" not in form:
        return oauth_error(400, "invalid_request", "Falta grant_type")
    if form["grant_type"] != "client_credentials":
        return oauth_error(400, "unsupported_grant_type", "Solo se admite client_credentials")
    if not form.get("scope"):
        return oauth_error(400, "invalid_request", "Falta scope")
    if not valid_credentials(client_credentials(request, form)):
        logger.warning("Pedido de token con credenciales inválidas")
        return oauth_error(401, "invalid_client", "Credenciales del cliente inválidas")

    expires_in = int(os.getenv("MOCK_TOKEN_EXPIRES_IN", "3600"))
    access_token = secrets.token_urlsafe(32)
    state.tokens.add(access_token)
    logger.info("Token emitido (expires_in=%s)", expires_in)
    return JSONResponse(
        {
            "token_type": "Bearer",
            "expires_in": expires_in,
            "ext_expires_in": expires_in,
            "access_token": access_token,
        }
    )


@app.delete("/kizuna/oauth/tokens", status_code=204)
async def revoke_tokens() -> Response:
    """Invalida los tokens emitidos (simula una revocación: el siguiente callback con ese token recibe 401)."""
    state.tokens.clear()
    return Response(status_code=204)


@app.get("/kizuna/received")
async def received() -> dict[str, Any]:
    """Payloads aceptados (200), total de peticiones (incluidas las que fallaron a propósito) y pedidos de token."""
    return {
        "total_requests": state.total_requests,
        "token_requests": state.token_requests,
        "received": state.received,
    }


@app.delete("/kizuna/received", status_code=204)
async def reset() -> Response:
    """Reinicia los contadores y la lista (útil entre pruebas). Los tokens emitidos siguen vigentes."""
    state.total_requests = 0
    state.token_requests = 0
    state.received.clear()
    return Response(status_code=204)
