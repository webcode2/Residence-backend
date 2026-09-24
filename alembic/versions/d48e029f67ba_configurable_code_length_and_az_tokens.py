"""add configurable token_code_length to estates and widen visitor token code length for AZ tokens

Revision ID: d48e029f67ba
Revises: b39c018d45fe
Create Date: 2026-09-24 20:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd48e029f67ba'
down_revision: Union[str, Sequence[str], None] = 'b39c018d45fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Add token_code_length to estates (default 6)
    op.add_column('estates', sa.Column('token_code_length', sa.Integer(), nullable=False, server_default='6'))

    # 2. Widen visitor_tokens.code and bookout_code from VARCHAR(4) to VARCHAR(16)
    op.alter_column('visitor_tokens', 'code', type_=sa.String(length=16), existing_type=sa.String(length=4), existing_nullable=False)
    op.alter_column('visitor_tokens', 'bookout_code', type_=sa.String(length=16), existing_type=sa.String(length=4), existing_nullable=True)

def downgrade() -> None:
    op.alter_column('visitor_tokens', 'bookout_code', type_=sa.String(length=4), existing_type=sa.String(length=16), existing_nullable=True)
    op.alter_column('visitor_tokens', 'code', type_=sa.String(length=4), existing_type=sa.String(length=16), existing_nullable=False)
    op.drop_column('estates', 'token_code_length')
