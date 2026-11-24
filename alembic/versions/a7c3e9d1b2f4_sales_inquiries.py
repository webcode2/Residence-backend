"""Enterprise sales inquiries from caretakers, reviewed in the owner console."""
from alembic import op
import sqlalchemy as sa
revision = 'a7c3e9d1b2f4'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('sales_inquiries',sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('created_at',sa.DateTime(),nullable=False),sa.Column('updated_at',sa.DateTime(),nullable=False),sa.Column('app_id',sa.String(50),nullable=False),sa.Column('user_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True),sa.Column('tier',sa.String(20),nullable=False),sa.Column('contact_name',sa.String(255),nullable=False),sa.Column('contact_email',sa.String(255),nullable=False),sa.Column('phone',sa.String(40),nullable=True),sa.Column('homes',sa.Integer(),nullable=True),sa.Column('message',sa.String(2000),nullable=False),sa.Column('status',sa.String(20),nullable=False),sa.Column('owner_note',sa.String(1000),nullable=True))
    op.create_index('ix_sales_inquiries_app_id','sales_inquiries',['app_id'])
    op.create_index('ix_sales_inquiries_status_history','sales_inquiries',['status','created_at','id'])

def downgrade():
    op.drop_table('sales_inquiries')
