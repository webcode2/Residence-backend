import uuid
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.models.user import User,UserRole
from app.models.sos import SOSAlert,SOSDelivery,PushDevice
from app.models.base import utc_now
from app.services import sos
from app.services.fcm import PushResult,PushStatus
from app.services.users import update_user_fcm_token
from app.dependencies import get_current_user
from app.main import app

@pytest.fixture
async def members(db_session):
    people=[]
    for i,(tenant,revoked) in enumerate([('SOS-ONE',False),('SOS-ONE',False),('SOS-ONE',True),('SOS-TWO',False)]):
        user=User(email=f'sos-{i}@example.com',full_name=f'Member {i}',hashed_password='not-used',roles=[UserRole.RESIDENT],app_id=tenant,house_number='12',street_name='Palm Street',is_revoked=revoked,fcm_token=f'token-{i}')
        db_session.add(user);people.append(user)
    await db_session.commit()
    db_session.add(PushDevice(token='second-phone',user_id=people[1].id))
    db_session.add(PushDevice(token='sender-second-phone',user_id=people[0].id))
    await db_session.commit()
    return people

@pytest.mark.asyncio
async def test_sos_is_estate_scoped_and_idempotent(db_session,members):
    result=await sos.create(db_session,members[0],uuid.uuid4())
    assert result['sender_name']=='Member 0' and result['house_location']=='12, Palm Street'
    deliveries=(await db_session.execute(select(SOSDelivery))).scalars().all()
    assert {x.token for x in deliveries}=={'token-1','second-phone'}
    assert result['devices_queued']==2
    retry=await sos.create(db_session,members[0],uuid.uuid4())
    assert retry['id']==result['id']
    assert len((await db_session.execute(select(SOSAlert))).scalars().all())==1

@pytest.mark.asyncio
async def test_outbox_payload_and_failure_retry(db_session,members,monkeypatch):
    await sos.create(db_session,members[0],uuid.uuid4())
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.SENT))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    assert await sos.drain(db_session)==2
    assert await sos.drain(db_session)==0
    payload=mock.call_args.args[3]
    assert payload['type']=='sos_live' and payload['app_id']=='SOS-ONE'
    assert payload['sender_name']=='Member 0'
    assert mock.call_args.args[4] is True

@pytest.mark.asyncio
async def test_revocation_after_queue_stops_push(db_session,members,monkeypatch):
    await sos.create(db_session,members[0],uuid.uuid4())
    members[1].is_revoked=True;await db_session.commit()
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.FAILED))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    await sos.drain(db_session)
    assert mock.await_count==0

@pytest.mark.asyncio
async def test_failed_receiver_delivery_is_retained_for_retry(db_session,members,monkeypatch):
    await sos.create(db_session,members[0],uuid.uuid4())
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.FAILED))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    await sos.drain(db_session)
    delivery=(await db_session.execute(select(SOSDelivery).where(SOSDelivery.token=='token-1'))).scalar_one()
    assert delivery.status=='failed' and delivery.attempts==1
    delivery.next_attempt_at=utc_now()-timedelta(seconds=1);await db_session.commit()
    await sos.drain(db_session)
    assert delivery.attempts==2

@pytest.mark.asyncio
async def test_cross_estate_reads_and_unauthorized_resolution(client,db_session,members):
    result=await sos.create(db_session,members[0],uuid.uuid4())
    app.dependency_overrides[get_current_user]=lambda:members[3]
    assert (await client.get('/api/v1/sos/'+result['id'])).status_code==404
    app.dependency_overrides[get_current_user]=lambda:members[1]
    assert (await client.post('/api/v1/sos/'+result['id']+'/resolve')).status_code==403
    app.dependency_overrides[get_current_user]=lambda:members[0]
    assert (await client.post('/api/v1/sos/'+result['id']+'/resolve')).json()['status']=='resolved'

@pytest.mark.asyncio
async def test_device_switch_removes_previous_owner(db_session,members):
    await update_user_fcm_token(db_session,members[0].id,'shared-phone')
    await update_user_fcm_token(db_session,members[1].id,'shared-phone')
    await db_session.refresh(members[0])
    assert members[0].fcm_token is None
    assert (await db_session.get(PushDevice,'shared-phone')).user_id==members[1].id

@pytest.mark.asyncio
async def test_logout_or_account_switch_cancels_queued_device_delivery(db_session,members,monkeypatch):
    await sos.create(db_session,members[0],uuid.uuid4())
    await update_user_fcm_token(db_session,members[3].id,'second-phone')
    members[1].fcm_token=None
    await db_session.commit()
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.SENT))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    await sos.drain(db_session)
    assert mock.await_count==0

def test_legacy_undated_alarm_is_downgraded_to_a_normal_report():
    message=sos.fcm_service._build_message('phone','SOS','Help',{'type':'sos_alert','alert_id':'test'},True)['message']
    assert message['data']['type']=='sos_missed'
    assert message['android']['notification']['channel_id']=='residence_sos_reports_v1'
    assert message['android']['notification']['sound']=='default'
    assert 'apns-expiration' in message['apns']['headers']

@pytest.mark.asyncio
async def test_old_queued_sender_delivery_is_skipped(db_session,members,monkeypatch):
    result=await sos.create(db_session,members[0],uuid.uuid4())
    own=SOSDelivery(alert_id=uuid.UUID(result['id']),user_id=members[0].id,token='token-0',status='pending',attempts=0,next_attempt_at=utc_now())
    db_session.add(own);await db_session.commit()
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.SENT))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    await sos.drain(db_session)
    assert {c.args[0] for c in mock.call_args_list}=={'token-1','second-phone'}
    assert own.status=='skipped'

@pytest.mark.asyncio
async def test_legacy_duplicate_sender_token_is_never_pushed(db_session,members,monkeypatch):
    members[1].fcm_token=members[0].fcm_token
    await db_session.commit()
    result=await sos.create(db_session,members[0],uuid.uuid4())
    deliveries=(await db_session.execute(select(SOSDelivery))).scalars().all()
    assert {d.token for d in deliveries}=={'second-phone'}
    # A legacy delivery created before the fix must also be suppressed.
    db_session.add(SOSDelivery(alert_id=uuid.UUID(result['id']),user_id=members[1].id,token=members[0].fcm_token,status='pending',attempts=0,next_attempt_at=utc_now()))
    await db_session.commit()
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.SENT))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    await sos.drain(db_session)
    assert [c.args[0] for c in mock.call_args_list]==['second-phone']


@pytest.mark.asyncio
async def test_receipt_suppresses_missed_report_and_unreachable_device_gets_report(db_session,members,monkeypatch):
    result=await sos.create(db_session,members[0],uuid.uuid4())
    alert=await db_session.get(SOSAlert,uuid.UUID(result['id']))
    mock=AsyncMock(side_effect=lambda token,*args:PushResult(token,PushStatus.SENT))
    monkeypatch.setattr(sos.fcm_service,'send_to_token',mock)
    await sos.drain(db_session)
    assert all(call.args[3]['type']=='sos_live' for call in mock.call_args_list)
    await sos.acknowledge(db_session,members[1],alert.id,'token-1')
    alert.created_at=utc_now()-timedelta(seconds=51)
    deliveries=(await db_session.execute(select(SOSDelivery))).scalars().all()
    for delivery in deliveries:delivery.next_attempt_at=utc_now()-timedelta(seconds=1)
    await db_session.commit()
    mock.reset_mock()
    await sos.drain(db_session)
    assert mock.await_count==1
    assert mock.call_args.args[0]=='second-phone'
    assert mock.call_args.args[3]['type']=='sos_missed'
    assert 'reported an SOS' in mock.call_args.args[2]

@pytest.mark.asyncio
async def test_receipt_cannot_acknowledge_another_estate_or_users_phone(db_session,members):
    from fastapi import HTTPException
    result=await sos.create(db_session,members[0],uuid.uuid4())
    with pytest.raises(HTTPException) as error:
        await sos.acknowledge(db_session,members[3],uuid.UUID(result['id']),'token-1')
    assert error.value.status_code==404

def test_live_push_expires_at_original_deadline_and_reports_use_normal_channel():
    from datetime import datetime,timezone
    now=datetime.now(timezone.utc)
    data={'type':'sos_live','alert_id':'test','alarm_expires_at':(now+timedelta(seconds=12)).isoformat()}
    message=sos.fcm_service._build_message('phone','SOS','Help',data,True)['message']
    assert 0<=int(message['android']['ttl'][:-1])<=12
    assert message['android']['notification']['channel_id']=='residence_sos_live_v2'
    assert message['apns']['headers']['apns-expiration']==str(int((now+timedelta(seconds=12)).timestamp()))
    data['alarm_expires_at']=(now-timedelta(seconds=1)).isoformat()
    late=sos.fcm_service._build_message('phone','SOS','Help',data,True)['message']
    assert late['data']['type']=='sos_missed'
    assert late['android']['notification']['sound']=='default'
    assert late['android']['notification']['channel_id']=='residence_sos_reports_v1'

@pytest.mark.asyncio
async def test_caretaker_restrictions_history_and_receiving(client,db_session,members):
    from app.models.admin_audit import AdminAuditLog
    caretaker=User(email='sos-caretaker@example.com',full_name='Caretaker',hashed_password='unused',roles=[UserRole.CARETAKER],app_id='SOS-ONE')
    db_session.add(caretaker);await db_session.commit()
    app.dependency_overrides[get_current_user]=lambda:members[0]
    assert (await client.get('/api/v1/sos/history')).status_code==403
    assert (await client.put(f'/api/v1/sos/restrictions/{members[1].id}',json={'disabled':True,'reason':'False alarms'})).status_code==403
    app.dependency_overrides[get_current_user]=lambda:caretaker
    assert (await client.put(f'/api/v1/sos/restrictions/{members[3].id}',json={'disabled':True,'reason':'False alarms'})).status_code==404
    assert (await client.put(f'/api/v1/sos/restrictions/{members[0].id}',json={'disabled':True,'reason':'Repeated false alarm reports'})).status_code==200
    app.dependency_overrides[get_current_user]=lambda:members[0]
    assert (await client.get('/api/v1/sos/permissions')).json()['can_trigger'] is False
    assert (await client.post('/api/v1/sos',json={'request_id':str(uuid.uuid4())})).status_code==403
    assert not (await db_session.execute(select(SOSAlert))).scalars().all()
    result=await sos.create(db_session,members[1],uuid.uuid4())
    assert members[0].id in (await db_session.execute(select(SOSDelivery.user_id))).scalars().all()
    await sos.create(db_session,members[3],uuid.uuid4())
    app.dependency_overrides[get_current_user]=lambda:caretaker
    page=(await client.get('/api/v1/sos/history')).json()
    assert len(page['alerts'])==1 and page['alerts'][0]['id']==result['id']
    assert len((await client.get('/api/v1/sos/restrictions')).json()['users'])==1
    assert (await client.put(f'/api/v1/sos/restrictions/{members[0].id}',json={'disabled':False,'reason':'Reviewed and restored'})).status_code==200
    app.dependency_overrides[get_current_user]=lambda:members[0]
    assert (await client.get('/api/v1/sos/permissions')).json()['can_trigger'] is True
    assert (await client.post('/api/v1/sos',json={'request_id':str(uuid.uuid4())})).status_code==200
    assert len((await db_session.execute(select(AdminAuditLog))).scalars().all())==2

@pytest.mark.asyncio
async def test_notification_inbox_offline_dedup_read_and_isolation(client,db_session,members):
    from app.services.notification_inbox import record
    members[1].fcm_token=None;await db_session.commit()
    await sos.create(db_session,members[0],uuid.uuid4())
    app.dependency_overrides[get_current_user]=lambda:members[1]
    response=(await client.get('/api/v1/notifications')).json()
    assert response['unread_count']==1
    item=response['items'][0]
    assert item['data']['type']=='sos_missed'
    assert (await client.post('/api/v1/notifications/'+item['id']+'/read')).json()['unread_count']==0
    await record(db_session,members[1].id,members[1].app_id,'sos:'+item['data']['alert_id'],'Duplicate','Retry',{})
    await db_session.commit()
    assert (await client.get('/api/v1/notifications')).json()['unread_count']==0
    app.dependency_overrides[get_current_user]=lambda:members[3]
    assert (await client.get('/api/v1/notifications')).json()['items']==[]
    assert (await client.post('/api/v1/notifications/'+item['id']+'/read')).status_code==404
    app.dependency_overrides[get_current_user]=lambda:members[1]
    await record(db_session,members[1].id,members[1].app_id,'chat:new','New message','Hello',{'type':'chat_message'})
    await db_session.commit()
    assert (await client.post('/api/v1/notifications/read-event',json={'event_key':'chat:new'})).json()['unread_count']==0
    await record(db_session,members[1].id,members[1].app_id,'chat:another','Another message','Hello',{})
    await db_session.commit()
    assert (await client.post('/api/v1/notifications/read-all')).json()['unread_count']==0


@pytest.mark.asyncio
async def test_removing_sos_sender_preserves_alert_history(db_session,members):
    from app.services.users import delete_user
    alert=await sos.create(db_session,members[0],uuid.uuid4())
    assert await delete_user(db_session,members[0].id,members[0].app_id)
    await db_session.refresh(members[0])
    assert members[0].is_revoked is True
    assert await db_session.get(SOSAlert,uuid.UUID(alert['id'])) is not None
