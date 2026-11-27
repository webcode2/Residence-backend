"""Officer welcome and password reset emails share the durable credential job."""

import sqlalchemy as sa
from alembic import op

revision = "b7c1d2e3f4a5"
down_revision = "a5f6320ce819"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("account_password_reset_jobs",
                  sa.Column("kind", sa.String(20), nullable=False, server_default="reset"))


def downgrade():
    op.drop_column("account_password_reset_jobs", "kind")
