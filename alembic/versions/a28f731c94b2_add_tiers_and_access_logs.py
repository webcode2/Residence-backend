"""add tiers and access logs

Revision ID: a28f731c94b2
Revises: c1a8d42e7b10
Create Date: 2026-09-24 19:50:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a28f731c94b2'
down_revision: Union[str, Sequence[str], None] = 'c1a8d42e7b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Add tier and quota columns to subscriptions
    op.add_column('subscriptions', sa.Column('tier', sa.String(length=50), nullable=False, server_default='standard'))
    op.add_column('subscriptions', sa.Column('monthly_verifications_limit', sa.Integer(), nullable=True, server_default='50000'))
    op.add_column('subscriptions', sa.Column('current_month_verifications', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('subscriptions', sa.Column('billing_cycle_start', sa.Date(), nullable=False, server_default=sa.func.current_date()))
    op.add_column('subscriptions', sa.Column('rfid_enabled', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('subscriptions', sa.Column('log_retention_days', sa.Integer(), nullable=False, server_default='90'))
    op.add_column('subscriptions', sa.Column('realtime_alerts_enabled', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('subscriptions', sa.Column('price_monthly', sa.Numeric(precision=10, scale=2), nullable=False, server_default='30.00'))

    # 2. Create access_logs table
    op.create_table(
        'access_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('app_id', sa.String(length=50), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('identifier', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('denial_reason', sa.String(length=255), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('resident_id', sa.Uuid(), nullable=True),
        sa.Column('visitor_name', sa.String(length=255), nullable=True),
        sa.Column('details', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['resident_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_access_logs_app_id_created_at', 'access_logs', ['app_id', 'created_at'], unique=False)
    op.create_index('ix_access_logs_app_id_status', 'access_logs', ['app_id', 'status'], unique=False)
    op.create_index('ix_access_logs_app_id_user_id', 'access_logs', ['app_id', 'user_id'], unique=False)

def downgrade() -> None:
    op.drop_index('ix_access_logs_app_id_user_id', table_name='access_logs')
    op.drop_index('ix_access_logs_app_id_status', table_name='access_logs')
    op.drop_index('ix_access_logs_app_id_created_at', table_name='access_logs')
    op.drop_table('access_logs')

    op.drop_column('subscriptions', 'price_monthly')
    op.drop_column('subscriptions', 'realtime_alerts_enabled')
    op.drop_column('subscriptions', 'log_retention_days')
    op.drop_column('subscriptions', 'rfid_enabled')
    op.drop_column('subscriptions', 'billing_cycle_start')
    op.drop_column('subscriptions', 'current_month_verifications')
    op.drop_column('subscriptions', 'monthly_verifications_limit')
    op.drop_column('subscriptions', 'tier')
