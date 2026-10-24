"""Online, resumable indexes for history cursors and live gate lookups."""
from alembic import op
import sqlalchemy as sa
revision='cd97b4c5d6e8'
down_revision='bc86a3b4c5d7'
branch_labels=None
depends_on=None

def upgrade():
    # Large existing tables need online index construction; IF NOT EXISTS also
    # makes a retry safe if an earlier concurrent build was interrupted.
    indexes=[
        ('ix_access_logs_estate_cursor','access_logs','app_id, created_at, id',''),
        ('ix_access_logs_estate_status_cursor','access_logs','app_id, status, created_at, id',''),
        ('ix_vis_tokens_live_entry','visitor_tokens','app_id, code, expires_at'," WHERE status = 'pending' AND is_used = false"),
        ('ix_vis_tokens_live_exit','visitor_tokens','app_id, bookout_code, bookout_expires_at'," WHERE status = 'checked_in'"),
        ('ix_vis_tokens_live_code','visitor_tokens','app_id, code, created_at, id'," WHERE status IN ('pending','checked_in')"),
        ('ix_vis_tokens_retention','visitor_tokens','app_id, status, expires_at, id',''),
        ('ix_vis_tokens_expiry_cleanup','visitor_tokens','expires_at, id'," WHERE status = 'pending'"),
        ('ix_vis_tokens_bookout_cleanup','visitor_tokens','bookout_expires_at, id'," WHERE bookout_code IS NOT NULL")]
    with op.get_context().autocommit_block():
        for name,table,columns,where in indexes:
            valid=op.get_bind().execute(sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(:name)"),{'name':name}).scalar()
            if valid is False:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns}){where}')

def downgrade():
    with op.get_context().autocommit_block():
        for name in ['ix_access_logs_estate_cursor','ix_access_logs_estate_status_cursor','ix_vis_tokens_live_entry','ix_vis_tokens_live_exit','ix_vis_tokens_live_code','ix_vis_tokens_retention','ix_vis_tokens_expiry_cleanup','ix_vis_tokens_bookout_cleanup']:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {name}')
