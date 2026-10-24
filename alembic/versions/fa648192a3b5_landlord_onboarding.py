"""Durable landlord onboarding, first-login passwords and host invitations."""
from alembic import op
import sqlalchemy as sa
revision = 'fa648192a3b5'
down_revision = 'ef53708192a4'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('users', sa.Column('phone', sa.String(40)))
    op.add_column('users', sa.Column('must_change_password', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('users', sa.Column('invitation_id', sa.Uuid()))
    op.add_column('users', sa.Column('relationship_type', sa.String(20)))
    op.add_column('registration_tokens', sa.Column('expires_at', sa.DateTime()))
    op.add_column('registration_tokens', sa.Column('relationship_type', sa.String(20), server_default='family', nullable=False))
    # Give existing unused invitations a finite migration grace period.
    op.execute("UPDATE registration_tokens SET expires_at = CURRENT_TIMESTAMP + INTERVAL '7 days' WHERE expires_at IS NULL")
    op.create_table('landlord_onboarding_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False), sa.Column('app_id', sa.String(50), nullable=False),
        sa.Column('batch_id', sa.Uuid(), nullable=False), sa.Column('email', sa.String(255), nullable=False),
        sa.Column('full_name', sa.String(255), nullable=False), sa.Column('phone', sa.String(40)),
        sa.Column('house_number', sa.String(20), nullable=False), sa.Column('street_name', sa.String(100)),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime()), sa.Column('lease_until', sa.DateTime()),
        sa.Column('last_error', sa.String(255)), sa.Column('password_ciphertext', sa.Text()),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id')),
        sa.UniqueConstraint('app_id','email',name='uq_landlord_onboarding_estate_email'))
    for name in ['app_id', 'batch_id', 'status', 'next_attempt_at']:
        op.create_index('ix_landlord_onboarding_jobs_'+name,'landlord_onboarding_jobs',[name])

def downgrade():
    op.drop_table('landlord_onboarding_jobs')
    for name in ['expires_at','relationship_type']:
        op.drop_column('registration_tokens',name)
    for name in ['phone','must_change_password','invitation_id','relationship_type']:
        op.drop_column('users',name)
