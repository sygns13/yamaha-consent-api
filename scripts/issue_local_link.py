"""Solo desarrollo local: emite un token nuevo para el enlace de una transacción y lo imprime.

En local el WhatsApp no se envía (TWILIO_SEND_ENABLED=false) y el token nunca se guarda ni se loguea, así que no hay
forma de conocerlo. Este script reemplaza el hash del enlace de la transacción por el de un token nuevo y muestra el
token y la URL de la landing, para probar con Postman o con la landing. El token anterior deja de servir.

Uso (desde yamaha-consent-api/, con el .env local):
    uv run python -m scripts.issue_local_link TRX-000123

No funciona con APP_ENV=production. Solo para registros principales PENDIENTE con su enlace sin usar.
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models import Consent, ConsentLink, ConsentStatus
from app.utils.tokens import generate_link_token


class IssueLinkError(Exception):
    pass


async def issue_link(transaction_id: str) -> str:
    async with SessionLocal.begin() as session:
        consent = await session.get(Consent, transaction_id)
        if consent is None:
            raise IssueLinkError(f"No existe la transacción {transaction_id}")
        if consent.source_transaction_id is not None:
            raise IssueLinkError(
                f"{transaction_id} es una copia vinculada (sin enlace): use el principal "
                f"{consent.source_transaction_id}"
            )
        if consent.status != ConsentStatus.PENDIENTE:
            raise IssueLinkError(f"{transaction_id} ya tiene respuesta ({consent.status.value})")
        link = await session.scalar(
            select(ConsentLink)
            .where(ConsentLink.transaction_id == transaction_id)
            .order_by(ConsentLink.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if link is None or link.used_at is not None:
            raise IssueLinkError(f"{transaction_id} no tiene un enlace sin usar")
        token, link.token_hash = generate_link_token()
    return token


async def main(transaction_id: str) -> int:
    settings = get_settings()
    if settings.app_env == "production":
        print("Este script es solo para desarrollo local (APP_ENV=production).", file=sys.stderr)
        return 2
    try:
        token = await issue_link(transaction_id)
    except IssueLinkError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    # A la consola, no al log: es el único lugar donde aparece el token.
    print(f"token={token}")
    print(f"url={settings.landing_base_url}/?t={token}")
    return 0


if __name__ == "__main__":
    # En la consola de Windows (cp1252) las tildes de los mensajes saldrían mal.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if len(sys.argv) != 2:
        print("Uso: uv run python -m scripts.issue_local_link <transaction_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
