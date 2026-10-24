"""Persist editable plans and immutable checkout quotes."""
from alembic import op
import sqlalchemy as sa
revision='cd315e6f7082'
down_revision='bc204d5e6f71'
branch_labels=None
depends_on=None

def upgrade():
    plans = op.create_table('subscription_plans',
        sa.Column('tier',sa.String(30),primary_key=True),
        sa.Column('name',sa.String(80),nullable=False),
        sa.Column('description',sa.String(1000),nullable=False),
        sa.Column('price_monthly',sa.Numeric(10,2),nullable=False),
        sa.Column('monthly_verifications_limit',sa.Integer(),nullable=True),
        sa.Column('rfid_enabled',sa.Boolean(),nullable=False),
        sa.Column('log_retention_days',sa.Integer(),nullable=False),
        sa.Column('realtime_alerts_enabled',sa.Boolean(),nullable=False),
        sa.Column('discount_6_months',sa.Numeric(5,2),nullable=False),
        sa.Column('discount_12_months',sa.Numeric(5,2),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.CheckConstraint('price_monthly > 0'),
        sa.CheckConstraint('discount_6_months >= 0 AND discount_6_months < 100'),
        sa.CheckConstraint('discount_12_months >= 0 AND discount_12_months < 100'),
    )
    op.create_table('subscription_checkouts',
        sa.Column('session_id',sa.String(255),primary_key=True),
        sa.Column('reference',sa.String(36),nullable=False,unique=True),
        sa.Column('app_id',sa.String(50),nullable=False),
        sa.Column('tier',sa.String(30),nullable=False),
        sa.Column('billing_months',sa.Integer(),nullable=False),
        sa.Column('amount',sa.Numeric(12,2),nullable=False),
        sa.Column('plan_snapshot',sa.JSON(),nullable=False),
        sa.Column('is_simulated',sa.Boolean(),nullable=False),
        sa.Column('fulfilled_at',sa.DateTime(),nullable=True),
    )
    op.create_index('ix_subscription_checkouts_app_id','subscription_checkouts',['app_id'])
    # Freeze current defaults at migration time; subsequent edits are stored in DB.
    defaults = [
      ('starter','Starter',15,20000,False,30,False,'Ideal for small estates using visitor passcodes only (no RFID hardware support).'),
      ('standard','Standard',30,50000,True,90,True,'Designed for mid-sized communities with RFID boom gates and real-time visitor alerts.'),
      ('premium','Premium',70,300000,True,180,True,'For high-traffic residential estates requiring 300k verifications and 6 months of audit logs.'),
      ('enterprise','Enterprise',100,None,True,365,True,'Full-scale access control with unlimited verifications, 1-year log retention, and instant alerts.'),
    ]
    op.bulk_insert(plans,[dict(tier=t,name=n,price_monthly=p,monthly_verifications_limit=l,rfid_enabled=r,log_retention_days=d,realtime_alerts_enabled=a,description=x,discount_6_months=0,discount_12_months=0,version=1) for t,n,p,l,r,d,a,x in defaults])

def downgrade():
    op.drop_table('subscription_checkouts')
    op.drop_table('subscription_plans')
