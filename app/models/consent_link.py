from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConsentLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Enlace único de la landing. Solo se guarda el SHA-256 del token; el token en claro nunca se guarda."""

    __tablename__ = "consent_links"

    transaction_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("consents.transaction_id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    # created_at + LINK_TTL_HOURS (lo calcula la aplicación).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Un solo uso: se marca al enviar las respuestas.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
