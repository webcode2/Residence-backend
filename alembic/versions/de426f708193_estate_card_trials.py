"""Card-backed estate trial activation; existing estates are unchanged."""
from alembic import op
import sqlalchemy as sa
revision = 'de426f708193'
down_revision = 'cd315e6f7082'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('estate_card_trials',
        sa.Column('app_id', sa.String(50), primary_key=True),
        sa.Column('session_id', sa.String(255), unique=True, nullable=True),
        sa.Column('product_id', sa.String(255), nullable=True),
        sa.Column('subscription_id', sa.String(255), unique=True, nullable=True),
        sa.Column('checkout_url', sa.String(1000), nullable=True),
        sa.Column('plan_snapshot', sa.JSON(), nullable=False),
        sa.Column('consent_at', sa.DateTime(), nullable=False),
        sa.Column('consent_user_id', sa.String(36), nullable=False),
        sa.Column('trial_days', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(50), nullable=False),
        sa.Column('period_end', sa.DateTime(), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False))

def downgrade():
    op.drop_table('estate_card_trials')
