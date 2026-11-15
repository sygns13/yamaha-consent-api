import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Dealer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Ficha del Dealer, registrada en el BackOffice y consultada por `dealerCode`.

    Relación con consents.dealer_code **lógica, sin FK**: consents puede traer códigos que aún no tienen ficha.
    `dealer_code` es único sin distinguir mayúsculas entre los vigentes (uq_dealers_active_code, sobre
    lower(dealer_code) WHERE is_deleted = 0) e inmutable tras crear. El RUC no es único (varias sedes lo comparten).
    """

    __tablename__ = "dealers"
    __table_args__ = (
        # Formato; el dígito verificador se valida en la aplicación (Anexo B).
        CheckConstraint("ruc ~ '^(10|15|16|17|20)[0-9]{9}$'", name="ruc_format"),
        CheckConstraint("is_deleted IN (0, 1)", name="is_deleted_bool"),
    )

    # Mismo largo y normalización que consents.dealer_code (solo trim).
    dealer_code: Mapped[str] = mapped_column(String(100), nullable=False)
    # Razón social (opcional: KIZUNA ya envía dealerName).
    business_name: Mapped[str | None] = mapped_column(String(250), nullable=True)
    ruc: Mapped[str] = mapped_column(CHAR(11), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(250), nullable=False)
    # Distrito INEI: determina provincia y departamento.
    district_ubigeo: Mapped[str] = mapped_column(
        CHAR(6), ForeignKey("ubigeo_districts.code"), nullable=False, index=True
    )
    contact_email: Mapped[str] = mapped_column(String(254), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # Concurrencia optimista (propuesta): cada actualización la incrementa.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    is_deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Autoría; nulos cuando actúa la CLI.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)


# Índice funcional: se declara fuera de la clase para poder usar lower() sobre la columna.
Index(
    "uq_dealers_active_code",
    func.lower(Dealer.dealer_code),
    unique=True,
    postgresql_where=text("is_deleted = 0"),
)
