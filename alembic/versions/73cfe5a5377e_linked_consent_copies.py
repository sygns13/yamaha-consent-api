"""linked consent copies

RN-04 con un ID de transacción nuevo: el nuevo ID se registra como copia vinculada del registro principal
(consents.source_transaction_id). El índice único parcial pasa a aplicarse solo a los registros principales.

Escrita a mano: autogenerate no detecta el CHECK ni el cambio de WHERE del índice parcial.

Downgrade: falla si ya existen copias vinculadas activas (violarían el índice anterior). Se deben eliminar antes.

Revision ID: 73cfe5a5377e
Revises: e4f8fc143c19
Create Date: 2026-10-27 01:20:35.072970

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '73cfe5a5377e'
down_revision: Union[str, Sequence[str], None] = 'e4f8fc143c19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UNIQUE_ACTIVE = 'uq_consents_active_document_dealer'
UNIQUE_COLUMNS = ['document_type', 'document_number', 'dealer']


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('consents', sa.Column('source_transaction_id', sa.String(length=100), nullable=True))
    op.create_foreign_key(
        op.f('fk_consents_source_transaction_id_consents'),
        'consents',
        'consents',
        ['source_transaction_id'],
        ['transaction_id'],
    )
    op.create_index(op.f('ix_consents_source_transaction_id'), 'consents', ['source_transaction_id'], unique=False)
    op.create_check_constraint(
        op.f('ck_consents_source_not_self'), 'consents', 'source_transaction_id <> transaction_id'
    )

    # Como máximo un registro PRINCIPAL activo por documento + Dealer (las copias vinculadas no cuentan).
    op.drop_index(UNIQUE_ACTIVE, table_name='consents', postgresql_where=sa.text('is_deleted = 0'))
    op.create_index(
        UNIQUE_ACTIVE,
        'consents',
        UNIQUE_COLUMNS,
        unique=True,
        postgresql_where=sa.text('is_deleted = 0 AND source_transaction_id IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        UNIQUE_ACTIVE,
        table_name='consents',
        postgresql_where=sa.text('is_deleted = 0 AND source_transaction_id IS NULL'),
    )
    op.create_index(UNIQUE_ACTIVE, 'consents', UNIQUE_COLUMNS, unique=True, postgresql_where=sa.text('is_deleted = 0'))

    op.drop_constraint(op.f('ck_consents_source_not_self'), 'consents', type_='check')
    op.drop_index(op.f('ix_consents_source_transaction_id'), table_name='consents')
    op.drop_constraint(op.f('fk_consents_source_transaction_id_consents'), 'consents', type_='foreignkey')
    op.drop_column('consents', 'source_transaction_id')
