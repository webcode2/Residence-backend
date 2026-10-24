"""Retain private landlord import originals until caretaker deletion."""
from alembic import op
import sqlalchemy as sa
revision = 'ab7592a3b4c6'
down_revision = 'fa648192a3b5'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('landlord_import_files',
        sa.Column('id',sa.Uuid(),primary_key=True), sa.Column('created_at',sa.DateTime(),nullable=False),
        sa.Column('updated_at',sa.DateTime(),nullable=False),sa.Column('app_id',sa.String(50),nullable=False),
        sa.Column('filename',sa.String(255),nullable=False),sa.Column('content_type',sa.String(100),nullable=False),
        sa.Column('content',sa.LargeBinary()),sa.Column('size_bytes',sa.Integer(),nullable=False),
        sa.Column('batch_id',sa.Uuid(),nullable=False),sa.Column('uploaded_by',sa.Uuid(),sa.ForeignKey('users.id')),
        sa.Column('deleted_at',sa.DateTime()))
    op.create_index('ix_landlord_import_files_app_id','landlord_import_files',['app_id'])
    op.create_index('ix_landlord_import_files_batch_id','landlord_import_files',['batch_id'])

def downgrade():
    op.drop_table('landlord_import_files')
