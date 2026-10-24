from datetime import timedelta
from sqlalchemy import select, delete
from app.models.estate import Estate, Subscription
from app.models.access_log import AccessLog
from app.models.base import utc_now

BATCH_SIZE = 1000

async def retention(db, app_id):
    value=(await db.execute(select(Subscription.log_retention_days,Subscription.tier)
        .join(Estate, Estate.id==Subscription.estate_id).where(Estate.app_id==app_id))).first()
    return (max(1,value[0]),value[1]) if value else (30,'starter')

async def delete_log_batch(db, app_id, cutoff, size=BATCH_SIZE, actor=None):
    ids=select(AccessLog.id).where(AccessLog.app_id==app_id,AccessLog.created_at<cutoff)
    ids=ids.order_by(AccessLog.created_at,AccessLog.id).limit(size).with_for_update(skip_locked=True)
    count=(await db.execute(delete(AccessLog).where(AccessLog.id.in_(ids)).execution_options(synchronize_session=False))).rowcount
    if count:
        from app.models.admin_audit import AdminAuditLog
        db.add(AdminAuditLog(actor_id=actor.id if actor else None,actor_email=actor.email if actor else 'system',
            action='history.retention.cleanup',target_type='estate_history',target_id=app_id,
            changes={'app_id':app_id,'deleted_count':count,'cutoff':cutoff.isoformat()}))
    await db.commit()
    return count
