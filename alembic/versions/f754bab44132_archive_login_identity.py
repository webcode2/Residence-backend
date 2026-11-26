"""Release retained account emails for new registration."""
from alembic import op
import sqlalchemy as sa

revision = "f754bab44132"
down_revision = "e6a97c4f10b2"


def upgrade():
    op.add_column("users", sa.Column("archive_email", sa.String(255), nullable=True))
    op.execute("""
        UPDATE users
        SET archive_email = email, email = 'archived-' || id::text || '@archive.residentpass.com'
        WHERE archived_at IS NOT NULL AND archive_hold_at IS NOT NULL
    """)


def downgrade():
    # If the address has been reused, the unique index prevents destructive rollback.
    op.execute("UPDATE users SET email = archive_email WHERE archive_email IS NOT NULL")
    op.drop_column("users", "archive_email")
