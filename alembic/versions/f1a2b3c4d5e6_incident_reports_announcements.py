"""Incident reports filed from the app and caretaker announcements."""
from alembic import op
import sqlalchemy as sa
revision = 'f1a2b3c4d5e6'
down_revision = 'de108f6a4b21'
branch_labels = None
depends_on = None

def base_columns():
    return [sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),sa.Column('updated_at',sa.DateTime(),nullable=False),sa.Column('app_id',sa.String(50),nullable=False)]

def upgrade():
    op.create_table('incident_reports',*base_columns(),sa.Column('reporter_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True),sa.Column('reporter_name',sa.String(255),nullable=True),sa.Column('reporter_role',sa.String(20),nullable=False),sa.Column('is_anonymous',sa.Boolean(),nullable=False),sa.Column('title',sa.String(120),nullable=False),sa.Column('description',sa.String(2000),nullable=False),sa.Column('location',sa.String(200),nullable=False),sa.Column('severity',sa.String(10),nullable=False),sa.Column('status',sa.String(20),nullable=False),sa.Column('resolution_note',sa.String(1000),nullable=True),sa.Column('handled_by',sa.Uuid(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True))
    op.create_index('ix_incident_reports_app_id','incident_reports',['app_id'])
    op.create_index('ix_incident_reports_estate_history','incident_reports',['app_id','created_at','id'])
    op.create_index('ix_incident_reports_reporter','incident_reports',['app_id','reporter_id','created_at'])
    op.create_table('announcements',*base_columns(),sa.Column('author_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True),sa.Column('author_name',sa.String(255),nullable=True),sa.Column('title',sa.String(120),nullable=False),sa.Column('body',sa.String(2000),nullable=False),sa.Column('category',sa.String(20),nullable=False),sa.Column('audience',sa.String(20),nullable=False),sa.Column('recipient_count',sa.Integer(),nullable=False),sa.Column('push_sent',sa.Integer(),nullable=False),sa.Column('push_failed',sa.Integer(),nullable=False))
    op.create_index('ix_announcements_app_id','announcements',['app_id'])
    op.create_index('ix_announcements_estate_history','announcements',['app_id','created_at','id'])

def downgrade():
    op.drop_table('announcements')
    op.drop_table('incident_reports')
