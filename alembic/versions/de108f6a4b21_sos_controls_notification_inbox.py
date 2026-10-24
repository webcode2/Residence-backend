"""Caretaker SOS sending restrictions and durable mobile notification inbox."""
from alembic import op
import sqlalchemy as sa
revision = 'de108f6a4b21'
down_revision = 'cd97b4c5d6e8'
branch_labels = None
depends_on = None

def base_columns():
    return [sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),sa.Column('updated_at',sa.DateTime(),nullable=False),sa.Column('app_id',sa.String(50),nullable=False)]

def upgrade():
    op.create_table('sos_restrictions',*base_columns(),sa.Column('user_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False,unique=True),sa.Column('disabled',sa.Boolean(),nullable=False),sa.Column('reason',sa.String(500),nullable=False),sa.Column('changed_by',sa.Uuid(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True))
    op.create_index('ix_sos_restrictions_app_id','sos_restrictions',['app_id'])
    op.create_index('ix_sos_restrictions_estate_user','sos_restrictions',['app_id','user_id'])
    op.create_table('app_notifications',*base_columns(),sa.Column('user_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),sa.Column('event_key',sa.String(200),nullable=False),sa.Column('title',sa.String(255),nullable=False),sa.Column('body',sa.String(2000),nullable=False),sa.Column('data',sa.JSON(),nullable=False),sa.Column('is_read',sa.Boolean(),nullable=False),sa.UniqueConstraint('user_id','event_key'))
    op.create_index('ix_app_notifications_app_id','app_notifications',['app_id'])
    op.create_index('ix_app_notifications_user_unread','app_notifications',['app_id','user_id','is_read','created_at'])
    op.create_index('ix_sos_alerts_estate_history','sos_alerts',['app_id','created_at','id'])

def downgrade():
    op.drop_index('ix_sos_alerts_estate_history',table_name='sos_alerts')
    op.drop_table('app_notifications')
    op.drop_table('sos_restrictions')
