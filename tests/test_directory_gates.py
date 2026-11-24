import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.chat import GateSite, ChatThread
from app.services.auth import create_access_token
from app.core.redis import set_token_live_status


@pytest.fixture
async def estate_users(db_session):
    estates = []
    for _ in range(2):
        estate = Estate(app_id=f'RP-{uuid.uuid4().hex[:10]}', name='Test estate')
        db_session.add(estate)
        await db_session.flush()
        db_session.add(Subscription(estate_id=estate.id, status='active', expiry_date=date.today()+timedelta(days=30)))
        users = {}
        for role in (UserRole.CARETAKER, UserRole.RESIDENT, UserRole.SECURITY):
            user = User(email=f'{uuid.uuid4().hex}@test.local', full_name=role.value, hashed_password='unused', roles=[role], app_id=estate.app_id)
            db_session.add(user)
            users[role.value] = user
        await db_session.flush()
        estates.append((estate, users))
    await db_session.commit()
    return estates


def headers(estate, user):
    return {'X-App-Id':estate.app_id, 'Authorization':'Bearer '+create_access_token({'sub':user.email,'app_id':estate.app_id,'roles':[r.value for r in user.roles]})}


@pytest.mark.asyncio
async def test_directory_is_tenant_scoped_and_caretaker_only(client, estate_users):
    estate, users = estate_users[0]
    response = await client.get('/api/v1/landlords/', headers=headers(estate,users['caretaker']))
    assert response.status_code == 200
    assert [row['id'] for row in response.json()] == [str(users['resident'].id)]
    assert response.json()[0]['occupants'] == []
    assert (await client.get('/api/v1/landlords/', headers=headers(estate,users['resident']))).status_code == 403


@pytest.mark.asyncio
async def test_gate_disable_enable_delete_retains_conversation(client, db_session, estate_users):
    estate, users = estate_users[0]
    admin = headers(estate,users['caretaker'])
    resident = headers(estate,users['resident'])
    response = await client.post('/api/v1/chat/gates', headers=admin,json={'name':'Service gate','code':'SERVICE'})
    assert response.status_code == 201, response.text
    gate_id = response.json()['id']
    # Keep a historical attribution and verify it survives deletion.
    thread = ChatThread(app_id=estate.app_id, resident_id=users['resident'].id, gate_site_id=uuid.UUID(gate_id))
    db_session.add(thread)
    await db_session.commit()
    assert (await client.patch(f'/api/v1/chat/gates/{gate_id}',headers=resident,json={'is_active':False})).status_code == 403
    other, other_users = estate_users[1]
    assert (await client.delete(f'/api/v1/chat/gates/{gate_id}',headers=headers(other,other_users['caretaker']))).status_code == 404
    assert (await client.patch(f'/api/v1/chat/gates/{gate_id}',headers=admin,json={'is_active':False})).status_code == 200
    assert (await client.get('/api/v1/chat/gates',headers=resident)).json() == []
    assert (await client.get('/api/v1/chat/gates?include_inactive=true',headers=admin)).json()[0]['is_active'] is False
    assert (await client.patch(f'/api/v1/chat/gates/{gate_id}',headers=admin,json={'is_active':True})).status_code == 200
    assert len((await client.get('/api/v1/chat/gates',headers=resident)).json()) == 1
    assert (await client.delete(f'/api/v1/chat/gates/{gate_id}',headers=admin)).status_code == 204
    assert (await client.get('/api/v1/chat/gates?include_inactive=true',headers=admin)).json() == []
    await db_session.refresh(thread)
    gate = await db_session.get(GateSite,uuid.UUID(gate_id))
    assert thread.gate_site_id == gate.id and gate.is_deleted
    assert (await client.patch(f'/api/v1/chat/gates/{gate_id}',headers=admin,json={'is_active':True})).status_code == 404


@pytest.mark.asyncio
async def test_visitor_history_tracks_live_entry_exit_and_ownership(client, db_session, estate_users, guard_headers):
    estate, users = estate_users[0]
    resident = headers(estate,users['resident'])
    created = await client.post('/api/v1/tokens/visitor',headers=resident,json={'visitor_name':'Lifecycle visitor'})
    assert created.status_code == 200,created.text
    item = created.json()
    await set_token_live_status(item['id'],'checked_in',extra={'checked_in_at':'2026-10-22T12:00:00'})
    history = (await client.get('/api/v1/tokens/visitor/history',headers=resident)).json()
    assert history[0]['status'] == 'checked_in' and history[0]['checked_in_at']
    other, other_users = estate_users[1]
    assert (await client.get('/api/v1/tokens/visitor/history',headers=headers(other,other_users['resident']))).json() == []
    guard = await guard_headers(estate.app_id)
    code = (await client.post(f"/api/v1/tokens/visitor/{item['id']}/book-out",headers=resident)).json()['bookout_code']
    with patch('app.api.v1.endpoints.verify._notify_visitor_access',new_callable=AsyncMock):
        exited = await client.get(f'/api/v1/verify/token/{code}?direction=exit',headers=guard)
    assert exited.status_code == 200 and exited.json()['action'] == 'book_out'
    assert (await client.get('/api/v1/tokens/visitor/history',headers=resident)).json()[0]['status'] == 'checked_out'
    assert (await client.get(f'/api/v1/verify/token/{code}?direction=exit',headers=guard)).status_code == 401


@pytest.mark.asyncio
async def test_visitor_access_push_is_tenant_scoped(db_session, estate_users):
    import asyncio
    from app.api.v1.endpoints.verify import _notify_visitor_access
    estate, users = estate_users[0]
    resident = users['resident']
    resident.fcm_token = 'test-device-token'
    await db_session.commit()
    result = {'action':'book_out','resident_id':str(resident.id),'message':'Exit authorized'}
    with patch('app.services.fcm.fcm_service.send_to_token',new_callable=AsyncMock) as send:
        await _notify_visitor_access(db_session,estate.app_id,result)
        await asyncio.sleep(0)
        assert send.await_count == 1
        assert send.call_args.kwargs['data']['type'] == 'visitor_access'
        await _notify_visitor_access(db_session,estate_users[1][0].app_id,result)
        await asyncio.sleep(0)
        assert send.await_count == 1


@pytest.mark.asyncio
async def test_generated_passcodes_stay_private_to_the_resident(client, estate_users, guard_headers):
    estate, users = estate_users[0]
    resident = headers(estate,users['resident'])
    waiting = (await client.post('/api/v1/tokens/visitor',headers=resident,json={'visitor_name':'Not here yet'})).json()
    arrived = (await client.post('/api/v1/tokens/visitor',headers=resident,json={'visitor_name':'At the gate'})).json()
    await set_token_live_status(arrived['id'],'checked_in',extra={'checked_in_at':'2026-10-22T12:00:00'})
    own = (await client.get('/api/v1/tokens/visitor/history',headers=resident)).json()
    assert {p['id'] for p in own} == {waiting['id'],arrived['id']} and all(p['code'] for p in own)
    for staff in (await guard_headers(estate.app_id), headers(estate,users['caretaker'])):
        seen = (await client.get('/api/v1/tokens/visitor/history',headers=staff)).json()
        # Staff see visitors who actually came through the gate, never pending passcodes or codes.
        assert [p['id'] for p in seen] == [arrived['id']]
        assert seen[0]['code'] == '' and seen[0]['bookout_code'] is None
        roster = (await client.get('/api/v1/tokens/visitor/active',headers=staff)).json()
        assert [p['id'] for p in roster] == [arrived['id']] and roster[0]['code'] == ''
