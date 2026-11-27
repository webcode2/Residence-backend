"""Caretaker-maintained details for security officers."""

import sqlalchemy as sa
from alembic import op

revision = "c8d2e3f4a5b6"
down_revision = "b7c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "security_staff_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("app_id", sa.String(50), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_number", sa.String(50), nullable=True),
        sa.Column("alternate_phone", sa.String(40), nullable=True),
        sa.Column("home_address", sa.String(500), nullable=True),
        sa.Column("emergency_contact_name", sa.String(255), nullable=True),
        sa.Column("emergency_contact_phone", sa.String(40), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_security_staff_profiles_user_id"),
    )
    op.create_index("ix_security_staff_profiles_app_id", "security_staff_profiles", ["app_id"])


def downgrade():
    op.drop_index("ix_security_staff_profiles_app_id", table_name="security_staff_profiles")
    op.drop_table("security_staff_profiles")
