"""add fcm_token to users

Revision ID: f82a937beba7
Revises: e82a637beba6
Create Date: 2026-09-30 10:48:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f82a937beba7'
down_revision: Union[str, Sequence[str], None] = 'd48e029f67ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column('users', sa.Column('fcm_token', sa.String(length=512), nullable=True))
    op.create_index(op.f('ix_users_fcm_token'), 'users', ['fcm_token'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_users_fcm_token'), table_name='users')
    op.drop_column('users', 'fcm_token')
