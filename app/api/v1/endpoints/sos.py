import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import get_current_user
from app.models.user import User, UserRole
from app.models.sos import SOSAlert, SOSRestriction, SOSDelivery
from app.models.base import utc_now
from app.models.admin_audit import AdminAuditLog
from app.services.history_policy import retention
from datetime import timedelta
from app.services import sos
from app.services.worker_wake import wake_worker
router=APIRouter()
class SOSRequest(BaseModel):
    request_id: uuid.UUID

@router.post('')
async def create_sos(body:SOSRequest,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    result = await sos.create(db,user,body.request_id)
    await wake_worker('sos')  # pushes are sent by the worker; respond immediately
    return result


def caretaker(user):
    if UserRole.CARETAKER not in user.roles:
        raise HTTPException(403, 'Estate caretaker access required.')

@router.get('/permissions')
async def permissions(db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    restriction = (await db.execute(select(SOSRestriction).where(SOSRestriction.user_id==user.id,SOSRestriction.app_id==user.app_id))).scalar_one_or_none()
    return {'can_trigger': not (restriction and restriction.disabled), 'reason': restriction.reason if restriction and restriction.disabled else None}

@router.get('/history')
async def history(offset:int=Query(0,ge=0,le=100000),limit:int=Query(50,ge=1,le=100),db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    caretaker(user)
    days,_ = await retention(db,user.app_id)
    alerts = (await db.execute(select(SOSAlert, SOSRestriction.disabled).outerjoin(SOSRestriction,(SOSRestriction.user_id==SOSAlert.sender_id)&(SOSRestriction.app_id==user.app_id)).where(SOSAlert.app_id==user.app_id,SOSAlert.created_at>=utc_now()-timedelta(days=days)).order_by(SOSAlert.created_at.desc(),SOSAlert.id.desc()).offset(offset).limit(limit+1))).all()
    counts={}
    ids=[a.id for a,_ in alerts[:limit]]
    if ids:
        rows=(await db.execute(select(SOSDelivery.alert_id,SOSDelivery.status,func.count()).where(SOSDelivery.alert_id.in_(ids)).group_by(SOSDelivery.alert_id,SOSDelivery.status))).all()
        for alert_id,status,count in rows: counts.setdefault(alert_id,{})[status]=count
    return {'alerts':[{**await sos.summary(db,a,counts.get(a.id,{})),'sending_disabled':bool(disabled)} for a,disabled in alerts[:limit]],'has_more':len(alerts)>limit,'retention_days':days}

@router.get('/restrictions')
async def restrictions(db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    caretaker(user)
    rows=(await db.execute(select(SOSRestriction,User).join(User,User.id==SOSRestriction.user_id).where(SOSRestriction.app_id==user.app_id,User.app_id==user.app_id,SOSRestriction.disabled.is_(True)).order_by(SOSRestriction.updated_at.desc()).limit(500))).all()
    return {'users':[{'id':str(u.id),'name':u.full_name,'house_location':', '.join(x for x in (u.house_number,u.street_name) if x),'reason':r.reason,'updated_at':r.updated_at.isoformat()+'Z'} for r,u in rows]}

class RestrictionRequest(BaseModel):
    disabled: bool
    reason: str = Field(min_length=3,max_length=500)

@router.put('/restrictions/{user_id}')
async def restrict(user_id:uuid.UUID,body:RestrictionRequest,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    caretaker(user)
    reason=body.reason.strip()
    if len(reason)<3: raise HTTPException(422,'Please provide a reason.')
    target=(await db.execute(select(User).where(User.id==user_id,User.app_id==user.app_id).with_for_update())).scalar_one_or_none()
    if not target: raise HTTPException(404,'User not found in your estate.')
    row=(await db.execute(select(SOSRestriction).where(SOSRestriction.user_id==target.id,SOSRestriction.app_id==user.app_id))).scalar_one_or_none()
    before=bool(row and row.disabled)
    if row is None:
        row=SOSRestriction(user_id=target.id,app_id=user.app_id,disabled=body.disabled,reason=reason,changed_by=user.id);db.add(row)
    else:
        row.disabled=body.disabled;row.reason=reason;row.changed_by=user.id
    db.add(AdminAuditLog(actor_id=user.id,actor_email=user.email,action='sos.sending.disabled' if body.disabled else 'sos.sending.restored',target_type='user',target_id=str(target.id),changes={'app_id':user.app_id,'before':before,'disabled':body.disabled,'reason':reason}))
    await db.commit()
    return {'user_id':str(target.id),'sending_disabled':body.disabled}

@router.get('/{alert_id}')
async def get_sos(alert_id:uuid.UUID,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    alert=(await db.execute(select(SOSAlert).where(SOSAlert.id==alert_id,SOSAlert.app_id==user.app_id))).scalar_one_or_none()
    if not alert: raise HTTPException(404,'SOS alert not found.')
    return await sos.summary(db,alert)

@router.post('/{alert_id}/resolve')
async def resolve_sos(alert_id:uuid.UUID,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
    alert=(await db.execute(select(SOSAlert).where(SOSAlert.id==alert_id,SOSAlert.app_id==user.app_id))).scalar_one_or_none()
    if not alert: raise HTTPException(404,'SOS alert not found.')
    if alert.sender_id!=user.id and not set(user.roles).intersection([UserRole.CARETAKER,UserRole.SECURITY]):
        raise HTTPException(403,'Only the sender, caretaker or security can resolve this alert.')
    alert.status='resolved';await db.commit()
    await wake_worker('sos')
    return await sos.summary(db,alert)


class SOSReceipt(BaseModel):
    fcm_token: str = Field(min_length=1, max_length=512)

@router.post('/{alert_id}/received')
async def receive_sos(alert_id: uuid.UUID, body: SOSReceipt, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await sos.acknowledge(db, user, alert_id, body.fcm_token)
