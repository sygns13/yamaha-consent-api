"""Mock de KIZUNA para pruebas locales (M5). No se despliega en Producción.

Es independiente del backend (no importa `app`): corre en su propio contenedor.

Variables de entorno (se leen en cada petición):
- MOCK_FAIL_TIMES=N: responde 500 a las primeras N peticiones a /kizuna/consent-result.
- MOCK_RESPONSE_DELAY_SECONDS=S: espera S segundos antes de responder (simula un timeout).
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("kizuna_mock")


class Consents(BaseModel):
    personal_data: bool
    # Requeridos pero nulos en NO_ACEPTADO (RN-01).
    marketing_yamaha: bool | None
    marketing_dealer: bool | None


class ConsentResult(BaseModel):
    """Payload propuesto en M5. TODO(PENDIENTE-13): reemplazar por el contrato real de KIZUNA."""

    transaction_id: str
    document_type: str
    document_number: str
    dealer: str
    status: Literal["ACEPTADO", "NO_ACEPTADO"]
    consents: Consents
    responded_at: datetime


class MockState:
    def __init__(self) -> None:
        self.total_requests = 0
        self.received: list[dict[str, Any]] = []


state = MockState()
app = FastAPI(title="KIZUNA mock", version="0.1.0")


def mask_document(value: str) -> str:
    """Mismo criterio que app/utils/masking.py: solo los últimos 4 caracteres visibles."""
    return "*" * len(value) if len(value) <= 4 else "*" * (len(value) - 4) + value[-4:]


@app.post("/kizuna/consent-result")
async def consent_result(payload: ConsentResult) -> JSONResponse:
    state.total_requests += 1
    attempt = state.total_requests

    delay = float(os.getenv("MOCK_RESPONSE_DELAY_SECONDS", "0"))
    if delay > 0:
        await asyncio.sleep(delay)

    fail_times = int(os.getenv("MOCK_FAIL_TIMES", "0"))
    if attempt <= fail_times:
        logger.warning(
            "Fallo simulado %s/%s para transaction_id=%s", attempt, fail_times, payload.transaction_id
        )
        return JSONResponse({"error": "fallo simulado"}, status_code=500)

    state.received.append(
        {"received_at": datetime.now(UTC).isoformat(), "payload": payload.model_dump(mode="json")}
    )
    logger.info(
        "Recibido transaction_id=%s document=%s status=%s",
        payload.transaction_id,
        mask_document(payload.document_number),
        payload.status,
    )
    return JSONResponse({"received": True})


@app.get("/kizuna/received")
async def received() -> dict[str, Any]:
    """Payloads aceptados (200) y total de peticiones, incluidas las que fallaron a propósito."""
    return {"total_requests": state.total_requests, "received": state.received}


@app.delete("/kizuna/received", status_code=204)
async def reset() -> Response:
    """Reinicia el contador y la lista (útil entre pruebas)."""
    state.total_requests = 0
    state.received.clear()
    return Response(status_code=204)
