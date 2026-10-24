"""Durable, per-account notification inbox; independent of push delivery."""
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from app.models.sos import AppNotification
from app.models.base import utc_now
import uuid

async def record(db, user_id, app_id, event_key, title, body, data):
    stmt=insert(AppNotification).values(id=uuid.uuid4(),created_at=utc_now(),updated_at=utc_now(),user_id=user_id,app_id=app_id,event_key=event_key,title=title[:255],body=body[:2000],data=data,is_read=False)
    await db.execute(stmt.on_conflict_do_nothing(index_elements=['user_id','event_key']))

async def unread_count(db,user_id,app_id):
    return (await db.execute(select(func.count()).select_from(AppNotification).where(AppNotification.user_id==user_id,AppNotification.app_id==app_id,AppNotification.is_read.is_(False)))).scalar_one()
