"""Persist estate SOS alerts, device registrations and push outbox."""
from alembic import op
import sqlalchemy as sa
revision='ef53708192a4'
down_revision='de426f708193'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('push_devices',sa.Column('token',sa.String(512),primary_key=True),sa.Column('user_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False))
    op.create_index('ix_push_devices_user_id','push_devices',['user_id'])
    op.create_table('sos_alerts',sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),sa.Column('updated_at',sa.DateTime(),nullable=False),sa.Column('app_id',sa.String(50),nullable=False),sa.Column('sender_id',sa.Uuid(),sa.ForeignKey('users.id'),nullable=False),sa.Column('request_id',sa.Uuid(),unique=True,nullable=False),sa.Column('sender_name',sa.String(255),nullable=False),sa.Column('house_location',sa.String(255),nullable=False),sa.Column('status',sa.String(20),nullable=False))
    op.create_index('ix_sos_alerts_app_id','sos_alerts',['app_id'])
    op.create_table('sos_deliveries',sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),sa.Column('updated_at',sa.DateTime(),nullable=False),sa.Column('alert_id',sa.Uuid(),sa.ForeignKey('sos_alerts.id',ondelete='CASCADE'),nullable=False),sa.Column('user_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),sa.Column('token',sa.String(512),nullable=False),sa.Column('status',sa.String(20),nullable=False),sa.Column('attempts',sa.Integer(),nullable=False),sa.Column('next_attempt_at',sa.DateTime(),nullable=False),sa.UniqueConstraint('alert_id','token'))
    op.create_index('ix_sos_deliveries_alert_id','sos_deliveries',['alert_id'])
    op.create_index('ix_sos_deliveries_status','sos_deliveries',['status'])
    op.execute("INSERT INTO push_devices (token,user_id) SELECT DISTINCT ON (fcm_token) fcm_token,id FROM users WHERE fcm_token IS NOT NULL AND fcm_token <> '' ORDER BY fcm_token,updated_at DESC")

def downgrade():
    op.drop_table('sos_deliveries')
    op.drop_table('sos_alerts')
    op.drop_table('push_devices')
