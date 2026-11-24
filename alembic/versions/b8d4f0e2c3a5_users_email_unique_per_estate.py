"""Make account emails unique per estate instead of platform-wide."""
from alembic import op
revision = 'b8d4f0e2c3a5'
down_revision = 'a7c3e9d1b2f4'
branch_labels = None
depends_on = None

def upgrade():
    op.drop_index('ix_users_email', table_name='users')
    op.create_index('ix_users_email', 'users', ['email'])
    op.drop_index('ix_users_app_id_email', table_name='users')
    op.create_index('ix_users_app_id_email', 'users', ['app_id', 'email'], unique=True)

def downgrade():
    # Fails if the same email now exists in more than one estate; resolve those accounts first.
    op.drop_index('ix_users_app_id_email', table_name='users')
    op.create_index('ix_users_app_id_email', 'users', ['app_id', 'email'])
    op.drop_index('ix_users_email', table_name='users')
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
