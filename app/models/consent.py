from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ConsentStatus, DocumentType, consent_status_enum, document_type_enum
from app.models.mixins import TimestampMixin


class Consent(TimestampMixin, Base):
    """Registro central, uno por transacción de KIZUNA.

    Documento + Dealer NO es único (RN-05): puede haber varios registros, pero como máximo uno activo
    (is_deleted = 0), garantizado por el índice único parcial uq_consents_active_document_dealer.
    """

    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint("is_deleted IN (0, 1)", name="is_deleted_bool"),
        Index(None, "document_type", "document_number", "dealer", "is_deleted"),
        Index(None, "created_at"),
        Index(
            "uq_consents_active_document_dealer",
            "document_type",
            "document_number",
            "dealer",
            unique=True,
            postgresql_where=text("is_deleted = 0"),
        ),
    )

    transaction_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    document_type: Mapped[DocumentType] = mapped_column(document_type_enum, nullable=False)
    document_number: Mapped[str] = mapped_column(String(20), nullable=False)
    # TODO(PENDIENTE-05): ¿KIZUNA envía código, nombre o ambos? Por ahora un solo campo.
    dealer: Mapped[str] = mapped_column(String(100), nullable=False)
    # Se anula en el borrado lógico (RN-01).
    full_name: Mapped[str | None] = mapped_column(String(250), nullable=True)
    # 9 dígitos sin prefijo de país. Se conserva siempre, también tras el rechazo.
    phone: Mapped[str] = mapped_column(CHAR(9), nullable=False)
    additional_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ConsentStatus] = mapped_column(
        consent_status_enum,
        nullable=False,
        index=True,
        default=ConsentStatus.PENDIENTE,
        server_default=ConsentStatus.PENDIENTE.value,
    )
    consent_personal_data: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    consent_marketing_yamaha: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    consent_marketing_dealer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 0 = vigente, 1 = borrado lógico (equivalente a TINYINT).
    is_deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
