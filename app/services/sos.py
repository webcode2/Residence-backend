import asyncio
import logging
import uuid
from datetime import timedelta
from sqlalchemy import select, or_, func, delete
from fastapi import HTTPException
from app.models.base import utc_now
from app.models.user import User
from app.models.sos import SOSAlert, SOSDelivery, PushDevice, SOSRestriction
from app.services.notification_inbox import record, unread_count
from app.services.fcm import fcm_service, PushStatus
from app.core.database import SessionLocal
from app.services.sos_policy import alarm_deadline, LIVE_DELIVERY_SECONDS, RECEIPT_GRACE_SECONDS, REPORT_RETENTION_HOURS

logger = logging.getLogger(__name__)
_worker = None

async def summary(db, alert, counts=None):
    if counts is None:
        counts = dict((await db.execute(select(SOSDelivery.status,func.count()).where(SOSDelivery.alert_id==alert.id).group_by(SOSDelivery.status))).all())
    return {'id':str(alert.id),'sender_id':str(alert.sender_id),'sender_name':alert.sender_name,'house_location':alert.house_location,'app_id':alert.app_id,'status':alert.status,'alarm_expires_at':alarm_deadline(alert).isoformat()+'Z','created_at':alert.created_at.isoformat()+'Z','devices_queued':sum(counts.values()),'delivery':counts,'push_configured':fcm_service.is_configured}

async def create(db, user, request_id):
    # Lock the sender to serialize retries and prevent accidental repeated broadcasts.
    sender=(await db.execute(select(User).where(User.id==user.id,User.app_id==user.app_id).with_for_update())).scalar_one()
    existing=(await db.execute(select(SOSAlert).where(SOSAlert.request_id==request_id))).scalar_one_or_none()
    if existing:
        if existing.sender_id!=user.id or existing.app_id!=user.app_id: raise HTTPException(409,'SOS request belongs to another user.')
        return await summary(db,existing)
    restriction = (await db.execute(select(SOSRestriction).where(SOSRestriction.user_id == sender.id, SOSRestriction.app_id == sender.app_id))).scalar_one_or_none()
    if restriction and restriction.disabled:
        raise HTTPException(403, 'SOS sending has been disabled by your estate caretaker. Contact them to review this restriction.')
    recent=(await db.execute(select(SOSAlert).where(SOSAlert.sender_id==sender.id,SOSAlert.status=='active',SOSAlert.created_at>utc_now()-timedelta(seconds=60)).order_by(SOSAlert.created_at.desc()).limit(1))).scalar_one_or_none()
    if recent: return await summary(db,recent)
    location=', '.join(x for x in [sender.house_number,sender.street_name] if x)
    if not location and sender.landlord_id:
        landlord=(await db.execute(select(User).where(User.id==sender.landlord_id,User.app_id==sender.app_id))).scalar_one_or_none()
        if landlord: location=', '.join(x for x in [landlord.house_number,landlord.street_name] if x)
    alert=SOSAlert(sender_id=sender.id,app_id=sender.app_id,request_id=request_id,sender_name=sender.full_name or 'Estate member',house_location=location or 'House location not set — contact the resident',status='active')
    db.add(alert)
    await db.flush()
    users=(await db.execute(select(User).where(User.app_id==sender.app_id,User.id!=sender.id,User.is_revoked.is_(False)))).scalars().all()
    for recipient in users:
        await record(db, recipient.id, sender.app_id, 'sos:'+str(alert.id), 'Estate SOS alert', f'{alert.sender_name} requested help at {alert.house_location}.', {'type':'sos_missed','alert_id':str(alert.id),'app_id':alert.app_id,'sender_id':str(alert.sender_id),'created_at':alert.created_at.isoformat()+'Z','sender_name':alert.sender_name,'house_location':alert.house_location})
    devices=(await db.execute(select(PushDevice).join(User,User.id==PushDevice.user_id).where(User.app_id==sender.app_id,User.id!=sender.id,User.is_revoked.is_(False)))).scalars().all()
    tokens={u.fcm_token:u.id for u in users if u.fcm_token}
    tokens.update({d.token:d.user_id for d in devices})
    own_tokens=set((await db.execute(select(PushDevice.token).where(PushDevice.user_id==sender.id))).scalars().all())
    if sender.fcm_token: own_tokens.add(sender.fcm_token)
    for token,user_id in tokens.items():
        if token in own_tokens: continue
        db.add(SOSDelivery(alert_id=alert.id,user_id=user_id,token=token,status='pending',attempts=0,next_attempt_at=utc_now()))
    await db.commit()
    return await summary(db,alert)

async def acknowledge(db, user, alert_id, token):
    delivery = (await db.execute(select(SOSDelivery).join(SOSAlert, SOSAlert.id == SOSDelivery.alert_id).where(
        SOSDelivery.alert_id == alert_id, SOSDelivery.user_id == user.id,
        SOSDelivery.token == token, SOSAlert.app_id == user.app_id).with_for_update())).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(404, 'SOS delivery not found')
    alert = await db.get(SOSAlert, alert_id)
    if utc_now() > alarm_deadline(alert) + timedelta(seconds=RECEIPT_GRACE_SECONDS):
        return {'received': False}
    if delivery.status not in ('report_sent', 'skipped'):
        delivery.status = 'received'
        await db.commit()
    return {'received': True}

async def drain(db, limit=50):
    deliveries=(await db.execute(select(SOSDelivery).where(SOSDelivery.status.in_(['pending','failed','awaiting_receipt','report_pending']),SOSDelivery.attempts<6,SOSDelivery.next_attempt_at<=utc_now()).order_by(SOSDelivery.created_at).limit(limit).with_for_update(skip_locked=True))).scalars().all()
    pending = []
    semaphore = asyncio.Semaphore(10)
    async def send_limited(token, title, body, payload):
        async with semaphore:
            return await fcm_service.send_to_token(token, title, body, payload, True)
    for delivery in deliveries:
        alert=await db.get(SOSAlert,delivery.alert_id)
        user=await db.get(User,delivery.user_id)
        device=await db.get(PushDevice,delivery.token)
        sender=await db.get(User,alert.sender_id) if alert else None
        if not alert or delivery.user_id==alert.sender_id or (sender and sender.fcm_token==delivery.token) or utc_now()-alert.created_at>timedelta(hours=REPORT_RETENTION_HOURS) or not user or user.is_revoked or user.app_id!=alert.app_id or (device is not None and device.user_id!=delivery.user_id) or (device is None and user.fcm_token!=delivery.token):
            delivery.status='skipped';continue
        live = alert.status == 'active' and utc_now() < alarm_deadline(alert)
        if live and delivery.status == 'awaiting_receipt':
            delivery.next_attempt_at = alarm_deadline(alert) + timedelta(seconds=RECEIPT_GRACE_SECONDS)
            continue
        payload={'type':'sos_live' if live else 'sos_missed','alert_id':str(alert.id),'app_id':alert.app_id,'sender_id':str(alert.sender_id),'sender_name':alert.sender_name,'house_location':alert.house_location,'created_at':alert.created_at.isoformat()+'Z','status':alert.status,'alarm_expires_at':alarm_deadline(alert).isoformat()+'Z'}
        payload['notification_count'] = await unread_count(db,user.id,user.app_id)
        title = 'SOS — Estate distress alarm' if live else 'Estate SOS report'
        body = f'{alert.sender_name} needs help at {alert.house_location}' if live else f"{alert.sender_name} reported an SOS at {alert.house_location} ({alert.created_at.strftime('%d %b, %H:%M UTC')}). Status: {alert.status}."
        pending.append((delivery, user, alert, live, send_limited(delivery.token, title, body, payload)))
    results = await asyncio.gather(*(item[4] for item in pending), return_exceptions=True)
    for (delivery, user, alert, live, _), result in zip(pending, results):
        if isinstance(result, BaseException):
            logger.error('SOS transport task failed: %s', type(result).__name__)
            delivery.status = 'failed' if live else 'report_pending'
            delivery.attempts += 1
            delivery.next_attempt_at = utc_now() + timedelta(seconds=5)
            continue
        delivery.attempts+=1
        if result.status in (PushStatus.SENT, PushStatus.SIMULATED):
            delivery.status = 'awaiting_receipt' if live else 'report_sent'
            delivery.next_attempt_at = alarm_deadline(alert) + timedelta(seconds=RECEIPT_GRACE_SECONDS)
        else:
            delivery.status = 'failed' if live else 'report_pending'
            delivery.next_attempt_at = min(utc_now()+timedelta(seconds=5*delivery.attempts), alarm_deadline(alert)) if live else utc_now()+timedelta(seconds=15)
        if result.status==PushStatus.UNREGISTERED:
            delivery.status='skipped'
            await db.execute(delete(PushDevice).where(PushDevice.token==delivery.token))
            if user.fcm_token==delivery.token: user.fcm_token=None
    await db.commit()
    return len(deliveries)

async def _loop():
    while True:
        try:
            async with SessionLocal() as db: await drain(db)
        except asyncio.CancelledError: raise
        except Exception: logger.exception('SOS outbox delivery failed; retained for retry')
        await asyncio.sleep(1)

def start_worker():
    global _worker
    if _worker is None or _worker.done(): _worker=asyncio.create_task(_loop(),name='sos_push_outbox')

async def stop_worker():
    global _worker
    if _worker:
        _worker.cancel()
        try: await _worker
        except asyncio.CancelledError: pass
        _worker=None
