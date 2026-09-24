"""add bookout fields to visitor tokens

Revision ID: b39c018d45fe
Revises: a28f731c94b2
Create Date: 2026-09-24 20:20:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b39c018d45fe'
down_revision: Union[str, Sequence[str], None] = 'a28f731c94b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column('visitor_tokens', sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'))
    op.add_column('visitor_tokens', sa.Column('checked_in_at', sa.DateTime(), nullable=True))
    op.add_column('visitor_tokens', sa.Column('checked_out_at', sa.DateTime(), nullable=True))
    op.add_column('visitor_tokens', sa.Column('bookout_code', sa.String(length=4), nullable=True))
    op.add_column('visitor_tokens', sa.Column('bookout_expires_at', sa.DateTime(), nullable=True))

    op.create_index('ix_vis_tokens_app_id_bookout_code', 'visitor_tokens', ['app_id', 'bookout_code'], unique=False)
    op.create_index('ix_vis_tokens_app_id_status', 'visitor_tokens', ['app_id', 'status'], unique=False)

def downgrade() -> None:
    op.drop_index('ix_vis_tokens_app_id_status', table_name='visitor_tokens')
    op.drop_index('ix_vis_tokens_app_id_bookout_code', table_name='visitor_tokens')
    op.drop_column('visitor_tokens', 'bookout_expires_at')
    op.drop_column('visitor_tokens', 'bookout_code')
    op.drop_column('visitor_tokens', 'checked_out_at')
    op.drop_column('visitor_tokens', 'checked_in_at')
    op.drop_column('visitor_tokens', 'status')
