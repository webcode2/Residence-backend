"""Track when a resident directory account was archived."""
from alembic import op
import sqlalchemy as sa

revision = "c3f80774a921"
down_revision = "b8d4f0e2c3a5"


def upgrade():
    op.add_column("users", sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("users", "archived_at")
