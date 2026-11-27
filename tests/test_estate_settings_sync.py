import uuid
from unittest.mock import AsyncMock
import pytest
from app.models.estate import Estate
from app.models.user import User, UserRole
from app.models.sos import PushDevice
from app.services.auth import create_access_token
from app.services import notification_queue
from app.services.fcm import fcm_service

@pytest.mark.asyncio
async def test_snapshot_and_admin_invalidation_are_estate_scoped(client, db_session, monkeypatch):
    app_id='SYNC-'+uuid.uuid4().hex[:8]
    other='OTHER-'+uuid.uuid4().hex[:8]
    db_session.add_all([Estate(app_id=app_id,name='Sync',is_active=True,token_code_length=6),Estate(app_id=other,name='Other',is_active=True)])
    users=[]
    for role,estate in [(UserRole.CARETAKER,app_id),(UserRole.RESIDENT,app_id),(UserRole.SECURITY,app_id),(UserRole.RESIDENT,other)]:
        user=User(email=uuid.uuid4().hex+'@example.com',app_id=estate,roles=[role],hashed_password='unused',active_session_id=uuid.uuid4().hex)
        db_session.add(user);users.append(user)
    await db_session.flush()
    for n,user in enumerate(users): db_session.add(PushDevice(token=f'sync-device-{n}',user_id=user.id))
    await db_session.commit()
    def headers(user):return {'X-App-Id':user.app_id,'Authorization':'Bearer '+create_access_token({'sub':user.email,'uid':str(user.id),'sid':user.active_session_id,'app_id':user.app_id})}
    notify=AsyncMock()
    monkeypatch.setattr(notification_queue, 'dispatch', notify)
    for user in users[:3]:
        response=await client.get('/api/v1/estates/mobile-settings',headers=headers(user))
        assert response.status_code==200,response.text
        assert response.json()=={'app_id':app_id,'estate_name':'Sync','token_code_length':6}
    denied=await client.patch('/api/v1/estates/settings',headers=headers(users[1]),json={'token_code_length':4})
    assert denied.status_code==403
    saved=await client.patch('/api/v1/estates/settings',headers=headers(users[0]),json={'token_code_length':4})
    assert saved.status_code==200,saved.text
    assert notify.call_args.args == ('settings_sync',)
    assert set(notify.call_args.kwargs['tokens']) == {'sync-device-0','sync-device-1','sync-device-2'}
    assert notify.call_args.kwargs['app_id'] == app_id
    fresh=await client.get('/api/v1/estates/mobile-settings',headers=headers(users[2]))
    assert fresh.json()['token_code_length']==4
    crossed=await client.get('/api/v1/estates/mobile-settings',headers={**headers(users[3]),'X-App-Id':app_id})
    assert crossed.status_code==401

def test_settings_event_is_silent_data_only():
    message=fcm_service._build_message('device','','',{'type':'estate_settings_changed','app_id':'A'},True)['message']
    assert 'notification' not in message
    assert 'notification' not in message['android']
    assert message['apns']['headers']['apns-push-type']=='background'
    assert 'sound' not in message['apns']['payload']['aps']
