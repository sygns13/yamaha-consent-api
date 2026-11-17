"""Referencias a usuarios en las respuestas del BackOffice (`UserRef`, sección 6.1): autoría de usuarios y dealers y
actor de la bitácora. Módulo aparte para que `audit.py` lo use sin importar `users.py` (que importa `audit.py`)."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.backoffice.common import UserRef


def full_name(first_names: str, last_names: str) -> str:
    return f"{first_names} {last_names}"


async def user_refs(db: AsyncSession, user_ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, UserRef]:
    """`UserRef` de cada id (también de usuarios eliminados: la autoría se conserva)."""
    wanted = {user_id for user_id in user_ids if user_id is not None}
    if not wanted:
        return {}
    rows = await db.execute(
        select(User.id, User.username, User.first_names, User.last_names).where(User.id.in_(wanted))
    )
    return {
        row.id: UserRef(
            id=row.id, username=row.username, full_name=full_name(row.first_names, row.last_names)
        )
        for row in rows
    }
