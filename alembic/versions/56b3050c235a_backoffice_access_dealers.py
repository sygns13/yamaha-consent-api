"""backoffice access dealers

BackOffice (requerimientos/BACKOFFICE_DEALERS.spec.md, secciones 4.3 a 4.5): roles, módulos y permisos (con sus
semillas), usuarios, sesiones, enlaces de restablecimiento, catálogo Ubigeo (vacío: lo carga la migración siguiente,
ubigeo_inei_seed), dealers y bitácora.

- No crea ningún usuario: el primer Administrador se crea con la CLI (sección 5.11).
- No toca las 7 tablas de v1.4. El downgrade elimina solo las 11 tablas del BackOffice (y sus datos).
- Después de migrar, en cada entorno, una vez: REVOKE UPDATE, DELETE ON audit_logs FROM consent_app; (sección 4.7; no va
  aquí porque consent_app puede no existir todavía en una base nueva).

Revision ID: 56b3050c235a
Revises: b7c2d9e41f60
Create Date: 2026-11-15 15:40:06.794804

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '56b3050c235a'
down_revision: Union[str, Sequence[str], None] = 'b7c2d9e41f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Semillas (sección 4.4): nombres de la sección 3, descripciones de los módulos = textos de las tarjetas de Inicio
# (Anexo A.2). roles.description queda nula (la sección 4.4 solo define código y nombre).
ROLES = [
    {'code': 'ADMIN', 'name': 'Administrador', 'description': None},
    {'code': 'OPERADOR', 'name': 'Operador', 'description': None},
]
MODULES = [
    {'code': 'DEALERS', 'name': 'Gestión de Dealers',
     'description': 'Registra y actualiza los datos de los Dealers.', 'sort_order': 10},
    {'code': 'USUARIOS', 'name': 'Administración de usuarios',
     'description': 'Crea y administra los usuarios del BackOffice.', 'sort_order': 20},
    {'code': 'AUDITORIA', 'name': 'Bitácora de auditoría',
     'description': 'Consulta las acciones registradas en el sistema.', 'sort_order': 30},
]
ROLE_MODULES = [
    {'role_code': 'ADMIN', 'module_code': 'DEALERS'},
    {'role_code': 'ADMIN', 'module_code': 'USUARIOS'},
    {'role_code': 'ADMIN', 'module_code': 'AUDITORIA'},
    {'role_code': 'OPERADOR', 'module_code': 'DEALERS'},
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('roles',
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=250), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_roles')),
    sa.UniqueConstraint('name', name=op.f('uq_roles_name'))
    )
    op.create_table('backoffice_modules',
    sa.Column('code', sa.String(length=30), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.String(length=250), nullable=True),
    sa.Column('sort_order', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_backoffice_modules'))
    )
    op.create_table('role_modules',
    sa.Column('role_code', sa.String(length=20), nullable=False),
    sa.Column('module_code', sa.String(length=30), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['module_code'], ['backoffice_modules.code'], name=op.f('fk_role_modules_module_code_backoffice_modules')),
    sa.ForeignKeyConstraint(['role_code'], ['roles.code'], name=op.f('fk_role_modules_role_code_roles')),
    sa.PrimaryKeyConstraint('role_code', 'module_code', name=op.f('pk_role_modules'))
    )

    roles = sa.table('roles', sa.column('code', sa.String), sa.column('name', sa.String),
                     sa.column('description', sa.String))
    modules = sa.table('backoffice_modules', sa.column('code', sa.String), sa.column('name', sa.String),
                       sa.column('description', sa.String), sa.column('sort_order', sa.SmallInteger))
    role_modules = sa.table('role_modules', sa.column('role_code', sa.String), sa.column('module_code', sa.String))
    op.bulk_insert(roles, ROLES)
    op.bulk_insert(modules, MODULES)
    op.bulk_insert(role_modules, ROLE_MODULES)

    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('role_code', sa.String(length=20), nullable=False),
    sa.Column('dni', sa.CHAR(length=8), nullable=False),
    sa.Column('first_names', sa.String(length=100), nullable=False),
    sa.Column('last_names', sa.String(length=100), nullable=False),
    sa.Column('username', sa.String(length=30), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('phone', sa.CHAR(length=9), nullable=True),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('failed_login_attempts', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('is_deleted', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('updated_by', sa.Uuid(), nullable=True),
    sa.Column('deleted_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("dni ~ '^[0-9]{8}$'", name=op.f('ck_users_dni_format')),
    sa.CheckConstraint("phone IS NULL OR phone ~ '^[0-9]{9}$'", name=op.f('ck_users_phone_format')),
    sa.CheckConstraint("username ~ '^[a-z][a-z0-9._-]{3,29}$'", name=op.f('ck_users_username_format')),
    sa.CheckConstraint('failed_login_attempts >= 0', name=op.f('ck_users_failed_login_attempts_non_negative')),
    sa.CheckConstraint('is_deleted IN (0, 1)', name=op.f('ck_users_is_deleted_bool')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_users_created_by_users')),
    sa.ForeignKeyConstraint(['deleted_by'], ['users.id'], name=op.f('fk_users_deleted_by_users')),
    sa.ForeignKeyConstraint(['role_code'], ['roles.code'], name=op.f('fk_users_role_code_roles')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_users_updated_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index(op.f('ix_users_role_code'), 'users', ['role_code'], unique=False)
    op.create_index('uq_users_active_dni', 'users', ['dni'], unique=True, postgresql_where=sa.text('is_deleted = 0'))
    op.create_index('uq_users_active_email', 'users', ['email'], unique=True, postgresql_where=sa.text('is_deleted = 0'))
    op.create_index('uq_users_active_username', 'users', ['username'], unique=True, postgresql_where=sa.text('is_deleted = 0'))
    op.create_table('user_sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.CHAR(length=64), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_reason', sa.String(length=30), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    op.create_index(op.f('ix_user_sessions_expires_at'), 'user_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)
    op.create_table('password_reset_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.CHAR(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('requested_ip', postgresql.INET(), nullable=True),
    sa.Column('requested_user_agent', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_password_reset_tokens_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_password_reset_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_password_reset_tokens_token_hash'))
    )
    op.create_index(op.f('ix_password_reset_tokens_user_id_created_at'), 'password_reset_tokens', ['user_id', 'created_at'], unique=False)
    op.create_table('ubigeo_departments',
    sa.Column('code', sa.CHAR(length=2), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.CheckConstraint("code ~ '^[0-9]{2}$'", name=op.f('ck_ubigeo_departments_code_format')),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_ubigeo_departments'))
    )
    op.create_table('ubigeo_provinces',
    sa.Column('code', sa.CHAR(length=4), nullable=False),
    sa.Column('department_code', sa.CHAR(length=2), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.CheckConstraint("code ~ '^[0-9]{4}$'", name=op.f('ck_ubigeo_provinces_code_format')),
    sa.CheckConstraint('left(code, 2) = department_code', name=op.f('ck_ubigeo_provinces_code_in_department')),
    sa.ForeignKeyConstraint(['department_code'], ['ubigeo_departments.code'], name=op.f('fk_ubigeo_provinces_department_code_ubigeo_departments')),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_ubigeo_provinces'))
    )
    op.create_index(op.f('ix_ubigeo_provinces_department_code'), 'ubigeo_provinces', ['department_code'], unique=False)
    op.create_table('ubigeo_districts',
    sa.Column('code', sa.CHAR(length=6), nullable=False),
    sa.Column('province_code', sa.CHAR(length=4), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.CheckConstraint("code ~ '^[0-9]{6}$'", name=op.f('ck_ubigeo_districts_code_format')),
    sa.CheckConstraint('left(code, 4) = province_code', name=op.f('ck_ubigeo_districts_code_in_province')),
    sa.ForeignKeyConstraint(['province_code'], ['ubigeo_provinces.code'], name=op.f('fk_ubigeo_districts_province_code_ubigeo_provinces')),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_ubigeo_districts'))
    )
    op.create_index(op.f('ix_ubigeo_districts_province_code'), 'ubigeo_districts', ['province_code'], unique=False)
    op.create_table('dealers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('dealer_code', sa.String(length=100), nullable=False),
    sa.Column('business_name', sa.String(length=250), nullable=True),
    sa.Column('ruc', sa.CHAR(length=11), nullable=False),
    sa.Column('address', sa.String(length=250), nullable=False),
    sa.Column('district_ubigeo', sa.CHAR(length=6), nullable=False),
    sa.Column('contact_email', sa.String(length=254), nullable=False),
    sa.Column('contact_phone', sa.String(length=20), nullable=True),
    sa.Column('contact_name', sa.String(length=150), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('is_deleted', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('updated_by', sa.Uuid(), nullable=True),
    sa.Column('deleted_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("ruc ~ '^(10|15|16|17|20)[0-9]{9}$'", name=op.f('ck_dealers_ruc_format')),
    sa.CheckConstraint('is_deleted IN (0, 1)', name=op.f('ck_dealers_is_deleted_bool')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_dealers_created_by_users')),
    sa.ForeignKeyConstraint(['deleted_by'], ['users.id'], name=op.f('fk_dealers_deleted_by_users')),
    sa.ForeignKeyConstraint(['district_ubigeo'], ['ubigeo_districts.code'], name=op.f('fk_dealers_district_ubigeo_ubigeo_districts')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_dealers_updated_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dealers'))
    )
    op.create_index(op.f('ix_dealers_district_ubigeo'), 'dealers', ['district_ubigeo'], unique=False)
    op.create_index(op.f('ix_dealers_ruc'), 'dealers', ['ruc'], unique=False)
    op.create_index('uq_dealers_active_code', 'dealers', [sa.literal_column('lower(dealer_code)')], unique=True, postgresql_where=sa.text('is_deleted = 0'))
    op.create_table('audit_logs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=50), nullable=False),
    sa.Column('entity_type', sa.String(length=30), nullable=True),
    sa.Column('entity_id', sa.String(length=100), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_logs_actor_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
    op.create_index(op.f('ix_audit_logs_actor_user_id_occurred_at'), 'audit_logs', ['actor_user_id', 'occurred_at'], unique=False)
    op.create_index(op.f('ix_audit_logs_entity_type_entity_id'), 'audit_logs', ['entity_type', 'entity_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_occurred_at'), 'audit_logs', ['occurred_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema: elimina las 11 tablas del BackOffice en orden inverso (las de v1.4 no se tocan)."""
    op.drop_index(op.f('ix_audit_logs_occurred_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_entity_type_entity_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_actor_user_id_occurred_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_action'), table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_index('uq_dealers_active_code', table_name='dealers', postgresql_where=sa.text('is_deleted = 0'))
    op.drop_index(op.f('ix_dealers_ruc'), table_name='dealers')
    op.drop_index(op.f('ix_dealers_district_ubigeo'), table_name='dealers')
    op.drop_table('dealers')
    op.drop_index(op.f('ix_ubigeo_districts_province_code'), table_name='ubigeo_districts')
    op.drop_table('ubigeo_districts')
    op.drop_index(op.f('ix_ubigeo_provinces_department_code'), table_name='ubigeo_provinces')
    op.drop_table('ubigeo_provinces')
    op.drop_table('ubigeo_departments')
    op.drop_index(op.f('ix_password_reset_tokens_user_id_created_at'), table_name='password_reset_tokens')
    op.drop_table('password_reset_tokens')
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_expires_at'), table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_index('uq_users_active_username', table_name='users', postgresql_where=sa.text('is_deleted = 0'))
    op.drop_index('uq_users_active_email', table_name='users', postgresql_where=sa.text('is_deleted = 0'))
    op.drop_index('uq_users_active_dni', table_name='users', postgresql_where=sa.text('is_deleted = 0'))
    op.drop_index(op.f('ix_users_role_code'), table_name='users')
    op.drop_table('users')
    op.drop_table('role_modules')
    op.drop_table('backoffice_modules')
    op.drop_table('roles')
