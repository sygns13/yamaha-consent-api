from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

# hide_parameters: los errores de SQLAlchemy (p. ej. IntegrityError) incluyen los parámetros del SQL en su mensaje,
# y ese mensaje llega a los logs en el traceback de un 500. Los parámetros traen datos personales.
engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, hide_parameters=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
