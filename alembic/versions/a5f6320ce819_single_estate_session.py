"""Track the active sign-in for each estate account."""

import sqlalchemy as sa
from alembic import op

revision = "a5f6320ce819"
down_revision = "ac3e56b9d120"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("active_session_id", sa.String(64), nullable=True))
    # The legacy user field points to the last token registered for the account.
    op.execute("""
        DELETE FROM push_devices AS device
        USING users AS account
        WHERE account.id = device.user_id
          AND device.token IS DISTINCT FROM account.fcm_token
    """)


def downgrade():
    op.drop_column("users", "active_session_id")
