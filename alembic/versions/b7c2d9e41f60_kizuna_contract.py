"""kizuna contract: customer_id, dealer_code, dealer_name, email, requested_at

Contrato definido por KIZUNA (v1.4 de la spec):
- Se elimina document_type: el cliente se identifica solo por customerId (document_number → customer_id).
- dealer → dealer_code (dealerCode) y se agregan dealer_name (dealerName), email (customerEmail) y requested_at
  (requestDateTime; en los registros existentes vale created_at).
- Los checks de un registro con respuesta nunca quedan nulos: el marketing de los rechazos (RN-01) pasa a false.
- Las notificaciones PENDIENTE se rearman con el payload nuevo (America/Lima no tiene horario de verano: -05:00).

Revision ID: b7c2d9e41f60
Revises: 94fef8d63ecf
Create Date: 2026-11-05 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7c2d9e41f60'
down_revision: Union[str, Sequence[str], None] = '94fef8d63ecf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_WHERE = sa.text('is_deleted = 0 AND source_transaction_id IS NULL')
OLD_UNIQUE = 'uq_consents_active_document_dealer'
NEW_UNIQUE = 'uq_consents_active_customer_dealer'
OLD_INDEX = 'ix_consents_document_type_document_number_dealer_is_deleted'
NEW_INDEX = 'ix_consents_customer_id_dealer_code_is_deleted'

document_type = postgresql.ENUM('DNI', 'RUC', 'CE', 'PASAPORTE', 'OTROS', name='document_type', create_type=False)

PENDING_PAYLOAD_SQL = """
UPDATE kizuna_notifications AS n
SET payload = jsonb_build_object(
        'IdentifierId', c.transaction_id,
        'customerId', c.customer_id,
        'dealerCode', c.dealer_code,
        'consentDateTime', to_char(c.responded_at AT TIME ZONE 'America/Lima', 'YYYY-MM-DD"T"HH24:MI:SS') || '-05:00',
        'personalDataConsent', COALESCE(c.consent_personal_data, false),
        'marketingConsentYMDP', COALESCE(c.consent_marketing_yamaha, false),
        'marketingConsentDEALER', COALESCE(c.consent_marketing_dealer, false)
    ),
    updated_at = now()
FROM consents AS c
WHERE c.transaction_id = n.transaction_id AND n.status = 'PENDIENTE'
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(OLD_UNIQUE, table_name='consents', postgresql_where=ACTIVE_WHERE)
    op.drop_index(OLD_INDEX, table_name='consents')

    op.alter_column('consents', 'document_number', new_column_name='customer_id')
    op.alter_column('consents', 'dealer', new_column_name='dealer_code')
    op.drop_column('consents', 'document_type')
    document_type.drop(op.get_bind(), checkfirst=True)

    op.add_column('consents', sa.Column('dealer_name', sa.String(length=150), nullable=True))
    op.add_column('consents', sa.Column('email', sa.String(length=254), nullable=True))
    op.add_column('consents', sa.Column('requested_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('UPDATE consents SET requested_at = created_at')
    op.alter_column('consents', 'requested_at', nullable=False)

    op.execute(
        "UPDATE consents SET consent_marketing_yamaha = COALESCE(consent_marketing_yamaha, false), "
        "consent_marketing_dealer = COALESCE(consent_marketing_dealer, false) WHERE status <> 'PENDIENTE'"
    )

    op.create_index(NEW_INDEX, 'consents', ['customer_id', 'dealer_code', 'is_deleted'], unique=False)
    op.create_index(
        NEW_UNIQUE, 'consents', ['customer_id', 'dealer_code'], unique=True, postgresql_where=ACTIVE_WHERE
    )

    op.execute(PENDING_PAYLOAD_SQL)


def downgrade() -> None:
    """Downgrade schema.

    No se recuperan el tipo de documento (todos quedan OTROS), ni los null del marketing de los rechazos, ni los
    payloads anteriores de las notificaciones.
    """
    op.drop_index(NEW_UNIQUE, table_name='consents', postgresql_where=ACTIVE_WHERE)
    op.drop_index(NEW_INDEX, table_name='consents')

    op.drop_column('consents', 'requested_at')
    op.drop_column('consents', 'email')
    op.drop_column('consents', 'dealer_name')

    document_type.create(op.get_bind(), checkfirst=True)
    op.add_column('consents', sa.Column('document_type', document_type, nullable=True))
    op.execute("UPDATE consents SET document_type = 'OTROS'")
    op.alter_column('consents', 'document_type', nullable=False)
    op.alter_column('consents', 'dealer_code', new_column_name='dealer')
    op.alter_column('consents', 'customer_id', new_column_name='document_number')

    op.create_index(
        OLD_INDEX, 'consents', ['document_type', 'document_number', 'dealer', 'is_deleted'], unique=False
    )
    op.create_index(
        OLD_UNIQUE,
        'consents',
        ['document_type', 'document_number', 'dealer'],
        unique=True,
        postgresql_where=ACTIVE_WHERE,
    )
