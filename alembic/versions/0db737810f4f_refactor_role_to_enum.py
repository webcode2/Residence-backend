"""Refactor role to enum

Revision ID: 0db737810f4f
Revises: ceb90e97239f
Create Date: 2026-01-20 00:15:18.185550

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0db737810f4f'
down_revision: Union[str, Sequence[str], None] = 'ceb90e97239f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create the enum type
    user_role_enum = postgresql.ENUM('saas_owner', 'caretaker', 'landlord', 'resident', 'security', name='user_role_enum')
    user_role_enum.create(op.get_bind())

    # 2. Alter column with cast
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE user_role_enum USING role::user_role_enum")
    
    # 3. Add default value setting if needed (SQLAlchemy handles it at app level but DB default is good)
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'resident'::user_role_enum")


def downgrade() -> None:
    # 1. Drop default
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")

    # 2. Alter column back to varchar
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(50) USING role::text")

    # 3. Drop the enum type
    op.execute("DROP TYPE user_role_enum")
