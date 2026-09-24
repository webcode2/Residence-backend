"""drop billings table

Revision ID: c1a8d42e7b10
Revises: b26b17f615ca
Create Date: 2026-09-24 19:38:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1a8d42e7b10'
down_revision: Union[str, Sequence[str], None] = 'b26b17f615ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.drop_index('ix_billings_app_id_user_id', table_name='billings', if_exists=True)
    op.drop_index(op.f('ix_billings_app_id'), table_name='billings', if_exists=True)
    op.drop_table('billings')

def downgrade() -> None:
    op.create_table(
        'billings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('app_id', sa.String(length=50), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('is_paid', sa.Boolean(), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_billings_app_id'), 'billings', ['app_id'], unique=False)
    op.create_index('ix_billings_app_id_user_id', 'billings', ['app_id', 'user_id'], unique=False)
