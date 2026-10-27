from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base de todos los modelos SQLAlchemy. Alembic usa Base.metadata para autogenerar migraciones."""
