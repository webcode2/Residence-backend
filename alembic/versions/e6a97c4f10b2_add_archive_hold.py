"""Track an explicit caretaker retention hold."""
from alembic import op
import sqlalchemy as sa

revision = "e6a97c4f10b2"
down_revision = "c3f80774a921"


def upgrade():
    op.add_column("users", sa.Column("archive_hold_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("users", "archive_hold_at")
