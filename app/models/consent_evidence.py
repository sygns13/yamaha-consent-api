import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConsentEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Evidencia del consentimiento. El borrado lógico (RN-01) no afecta esta tabla."""

    __tablename__ = "consent_evidence"

    transaction_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("consents.transaction_id"), nullable=False, index=True
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("consent_links.id"), nullable=False, index=True
    )
    # Primera IP de X-Forwarded-For.
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_url: Mapped[str] = mapped_column(Text, nullable=False)
    # {"personal_data": bool, "marketing_yamaha": bool, "marketing_dealer": bool}
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
