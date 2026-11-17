"""Modelos SQLAlchemy. Importar este paquete registra todas las tablas en Base.metadata (lo usa Alembic)."""

from app.models.audit_log import AuditAction, AuditEntityType, AuditLog
from app.models.consent import Consent
from app.models.consent_evidence import ConsentEvidence
from app.models.consent_link import ConsentLink
from app.models.consent_request import ACTION_CREATED, ACTION_UPDATED, ConsentRequest
from app.models.dealer import Dealer
from app.models.enums import ConsentStatus, NotificationStatus, WhatsappStatus
from app.models.kizuna_notification import KizunaNotification
from app.models.password_reset_token import PasswordResetToken
from app.models.role import BackofficeModule, ModuleCode, Role, RoleCode, RoleModule
from app.models.ubigeo import UbigeoDepartment, UbigeoDistrict, UbigeoProvince
from app.models.user import User, UserStatus
from app.models.user_session import SessionRevokedReason, UserSession
from app.models.whatsapp import WhatsappMessage, WhatsappMessageEvent

# Tablas cuyas filas cargan las migraciones (catálogos del BackOffice, sección 4.4). Los tests no las vacían entre un
# test y otro (tests/conftest.py); ninguna referencia a una tabla que se vacía.
SEED_TABLES = frozenset(
    {
        "roles",
        "backoffice_modules",
        "role_modules",
        "ubigeo_departments",
        "ubigeo_provinces",
        "ubigeo_districts",
    }
)

__all__ = [
    "ACTION_CREATED",
    "ACTION_UPDATED",
    "SEED_TABLES",
    "AuditAction",
    "AuditEntityType",
    "AuditLog",
    "BackofficeModule",
    "Consent",
    "ConsentEvidence",
    "ConsentLink",
    "ConsentRequest",
    "ConsentStatus",
    "Dealer",
    "KizunaNotification",
    "ModuleCode",
    "NotificationStatus",
    "PasswordResetToken",
    "Role",
    "RoleCode",
    "RoleModule",
    "SessionRevokedReason",
    "UbigeoDepartment",
    "UbigeoDistrict",
    "UbigeoProvince",
    "User",
    "UserSession",
    "UserStatus",
    "WhatsappMessage",
    "WhatsappMessageEvent",
    "WhatsappStatus",
]
