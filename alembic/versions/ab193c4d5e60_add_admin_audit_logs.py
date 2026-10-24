"""Preserve platform-owner management actions."""
from alembic import op
import sqlalchemy as sa
revision = 'ab193c4d5e60'
down_revision = 'a7c3e9d1f204'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('admin_audit_logs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('actor_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('actor_email', sa.String(255), nullable=False),
        sa.Column('action', sa.String(80), nullable=False),
        sa.Column('target_type', sa.String(40), nullable=False),
        sa.Column('target_id', sa.String(100), nullable=False),
        sa.Column('changes', sa.JSON(), nullable=False))
    op.create_index('ix_admin_audit_created_at', 'admin_audit_logs', ['created_at'])


def downgrade():
    op.drop_index('ix_admin_audit_created_at', table_name='admin_audit_logs')
    op.drop_table('admin_audit_logs')
