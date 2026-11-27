"""Fingerprint / Face ID sign-in keys for resident phones."""

import sqlalchemy as sa
from alembic import op

revision = "d9e3f4a5b6c7"
down_revision = "c8d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_sign_in_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("app_id", sa.String(50), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("password_fingerprint", sa.String(64), nullable=False),
        sa.Column("device_label", sa.String(100), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    for field in ("app_id", "user_id"):
        op.create_index(f"ix_device_sign_in_keys_{field}", "device_sign_in_keys", [field])


def downgrade():
    op.drop_table("device_sign_in_keys")
