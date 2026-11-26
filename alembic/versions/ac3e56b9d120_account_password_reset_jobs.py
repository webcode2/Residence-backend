"""Persist generated credential delivery until completed by the worker."""
from alembic import op
import sqlalchemy as sa

revision = "ac3e56b9d120"
down_revision = "f754bab44132"


def upgrade():
    op.create_table(
        "account_password_reset_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("app_id", sa.String(50), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("previous_password_fingerprint", sa.String(64), nullable=False),
        sa.Column("password_ciphertext", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
    )
    for field in ("app_id", "user_id", "next_attempt_at"):
        op.create_index(f"ix_account_password_reset_jobs_{field}", "account_password_reset_jobs", [field])


def downgrade():
    op.drop_table("account_password_reset_jobs")
