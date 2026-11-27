import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.sos import AppNotification
from app.services.notification_inbox import unread_count
router=APIRouter()

@router.get('')
async def inbox(offset:int=Query(0,ge=0,le=100000),limit:int=Query(50,ge=1,le=100),db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    rows=(await db.execute(select(AppNotification).where(AppNotification.user_id==user.id,AppNotification.app_id==user.app_id).order_by(AppNotification.created_at.desc(),AppNotification.id.desc()).offset(offset).limit(limit+1))).scalars().all()
    return {'unread_count':await unread_count(db,user.id,user.app_id),'has_more':len(rows)>limit,'items':[{'id':str(r.id),'title':r.title,'body':r.body,'data':r.data,'is_read':r.is_read,'created_at':r.created_at.isoformat()+'Z'} for r in rows[:limit]]}

@router.post('/read-all')
async def read_all(db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    await db.execute(update(AppNotification).where(AppNotification.user_id==user.id,AppNotification.app_id==user.app_id,AppNotification.is_read.is_(False)).values(is_read=True));await db.commit()
    return {'unread_count':await unread_count(db,user.id,user.app_id)}

@router.get('/announcements/{announcement_id}')
async def get_announcement_notification(
    announcement_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = await db.scalar(select(AppNotification).where(
        AppNotification.user_id == user.id,
        AppNotification.app_id == user.app_id,
        AppNotification.event_key == f'announcement:{announcement_id}',
    ))
    if item is None:
        raise HTTPException(404, 'Notification not found.')
    return {'id': str(item.id), 'title': item.title, 'body': item.body, 'data': item.data}


class ReadEvent(BaseModel):
    event_key: str = Field(min_length=1,max_length=200)

@router.post('/read-event')
async def read_event(body:ReadEvent,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    await db.execute(update(AppNotification).where(AppNotification.user_id==user.id,AppNotification.app_id==user.app_id,AppNotification.event_key==body.event_key).values(is_read=True));await db.commit()
    return {'unread_count':await unread_count(db,user.id,user.app_id)}

@router.post('/{notification_id}/read')
async def read(notification_id:uuid.UUID,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    result=await db.execute(update(AppNotification).where(AppNotification.id==notification_id,AppNotification.user_id==user.id,AppNotification.app_id==user.app_id).values(is_read=True))
    if not result.rowcount: raise HTTPException(404,'Notification not found.')
    await db.commit()
    return {'unread_count':await unread_count(db,user.id,user.app_id)}
