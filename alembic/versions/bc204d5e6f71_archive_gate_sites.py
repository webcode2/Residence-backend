"""Archive deleted gates while retaining conversation attribution."""
from alembic import op
import sqlalchemy as sa
revision='bc204d5e6f71'
down_revision='ab193c4d5e60'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('gate_sites',sa.Column('is_deleted',sa.Boolean(),nullable=False,server_default=sa.false()))

def downgrade():
    op.drop_column('gate_sites','is_deleted')
