"""Queued history exports/purges and indexes for history and live verification."""
from alembic import op
import sqlalchemy as sa
revision='bc86a3b4c5d7'
down_revision='ab7592a3b4c6'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('history_jobs',sa.Column('id',sa.Uuid(),primary_key=True),
        sa.Column('created_at',sa.DateTime(),nullable=False),sa.Column('updated_at',sa.DateTime(),nullable=False),
        sa.Column('app_id',sa.String(50),nullable=False),sa.Column('kind',sa.String(20),nullable=False),
        sa.Column('status',sa.String(30),nullable=False),sa.Column('actor_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='SET NULL')),
        sa.Column('actor_email',sa.String(255),nullable=False),sa.Column('start_at',sa.DateTime(),nullable=False),
        sa.Column('end_at',sa.DateTime(),nullable=False),sa.Column('snapshot_at',sa.DateTime(),nullable=False),
        sa.Column('status_filter',sa.String(20)),sa.Column('retention_days',sa.Integer(),nullable=False),
        sa.Column('preview_count',sa.Integer(),nullable=False),sa.Column('processed_count',sa.Integer(),nullable=False),
        sa.Column('cursor_at',sa.DateTime()),sa.Column('cursor_id',sa.Uuid()),sa.Column('attempts',sa.Integer(),nullable=False),
        sa.Column('next_attempt_at',sa.DateTime()),sa.Column('last_error',sa.String(255)),sa.Column('expires_at',sa.DateTime()),sa.Column('confirmed_at',sa.DateTime()))
    op.create_index('ix_history_jobs_app_id','history_jobs',['app_id'])
    op.create_index('ix_history_jobs_status','history_jobs',['status'])
    op.create_index('ix_history_jobs_purge_range','history_jobs',['app_id','end_at'],postgresql_where=sa.text("kind = 'purge' AND confirmed_at IS NOT NULL"))
    op.create_table('history_export_chunks',sa.Column('job_id',sa.Uuid(),sa.ForeignKey('history_jobs.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('sequence',sa.Integer(),primary_key=True),sa.Column('content',sa.LargeBinary(),nullable=False))

def downgrade():
    op.drop_table('history_export_chunks')
    op.drop_table('history_jobs')
