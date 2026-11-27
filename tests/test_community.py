import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select
from app.dependencies import get_current_user
from app.main import app
from app.models.sos import AppNotification, PushDevice
from app.models.user import User, UserRole
from app.services import community
from app.services.fcm import PushResult, PushStatus

REPORT = {'title': 'Broken gate light', 'description': 'The north gate light has been out since Monday.',
          'location': 'North gate', 'severity': 'medium'}


@pytest.fixture
async def estate(db_session):
    people = {}
    for key, roles, tenant in [('caretaker', [UserRole.CARETAKER], 'COMM-ONE'), ('resident', [UserRole.RESIDENT], 'COMM-ONE'),
                               ('neighbour', [UserRole.RESIDENT], 'COMM-ONE'), ('host', [UserRole.LANDLORD], 'COMM-ONE'), ('independent', [UserRole.RESIDENT], 'COMM-ONE'), ('guard', [UserRole.SECURITY], 'COMM-ONE'),
                               ('outsider', [UserRole.CARETAKER], 'COMM-TWO')]:
        people[key] = User(email=f'{key}@community.example', full_name=key.title(), hashed_password='not-used',
                           roles=roles, app_id=tenant)
        db_session.add(people[key])
    await db_session.commit()
    for name in ('resident', 'neighbour'):
        people[name].landlord_id = people['host'].id
    db_session.add(PushDevice(token='resident-phone', user_id=people['resident'].id))
    await db_session.commit()
    return people


@pytest.fixture
def act_as():
    def use(user):
        app.dependency_overrides[get_current_user] = lambda: user
    yield use
    app.dependency_overrides.pop(get_current_user, None)


async def file_report(client, act_as, user, **changes):
    act_as(user)
    response = await client.post('/api/v1/reports', json={**REPORT, **changes})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_resident_report_reaches_caretaker_inbox(client, db_session, estate, act_as):
    report = await file_report(client, act_as, estate['resident'])
    assert report['status'] == 'open' and report['reported_by_name'] == 'Resident' and report['is_own']
    inbox = (await db_session.execute(select(AppNotification).where(AppNotification.user_id == estate['caretaker'].id))).scalars().all()
    assert [n.event_key for n in inbox] == [f"incident:{report['id']}"]
    outsider_inbox = (await db_session.execute(select(AppNotification).where(AppNotification.user_id == estate['outsider'].id))).scalars().all()
    assert outsider_inbox == []


@pytest.mark.asyncio
async def test_anonymous_report_shows_identity_only_to_authorized_caretaker(client, estate, act_as):
    await file_report(client, act_as, estate['resident'], is_anonymous=True)
    act_as(estate['caretaker'])
    listed = (await client.get('/api/v1/reports')).json()
    assert listed['reports'][0]['reported_by_name'] == 'Anonymous' and listed['counts']['open'] == 1
    assert listed['reports'][0]['reporter_identity'] == 'Resident'
    assert listed['reports'][0]['reporter_id'] == str(estate['resident'].id)
    act_as(estate['resident'])
    own = (await client.get('/api/v1/reports')).json()
    assert own['reports'][0]['reported_by_name'] == 'You (anonymous)' and own['reports'][0]['is_own']
    assert 'reporter_identity' not in own['reports'][0]
    assert 'reporter_id' not in own['reports'][0]


@pytest.mark.asyncio
async def test_members_only_see_their_own_reports_and_estates_are_isolated(client, estate, act_as):
    await file_report(client, act_as, estate['resident'])
    act_as(estate['neighbour'])
    assert (await client.get('/api/v1/reports')).json()['reports'] == []
    act_as(estate['outsider'])
    assert (await client.get('/api/v1/reports')).json()['reports'] == []


@pytest.mark.asyncio
async def test_only_caretaker_of_the_estate_can_triage(client, db_session, estate, act_as):
    report = await file_report(client, act_as, estate['resident'])
    act_as(estate['resident'])
    assert (await client.patch(f"/api/v1/reports/{report['id']}", json={'status': 'resolved'})).status_code == 403
    act_as(estate['outsider'])
    assert (await client.patch(f"/api/v1/reports/{report['id']}", json={'status': 'resolved'})).status_code == 404
    act_as(estate['caretaker'])
    response = await client.patch(f"/api/v1/reports/{report['id']}", json={'status': 'resolved', 'resolution_note': 'Bulb replaced.'})
    assert response.status_code == 200 and response.json()['status'] == 'resolved'
    keys = (await db_session.execute(select(AppNotification.event_key).where(AppNotification.user_id == estate['resident'].id))).scalars().all()
    assert f"incident:{report['id']}:resolved" in keys


@pytest.mark.asyncio
async def test_report_validation_and_rate_limit(client, estate, act_as, monkeypatch):
    act_as(estate['resident'])
    assert (await client.post('/api/v1/reports', json={**REPORT, 'severity': 'critical'})).status_code == 422
    assert (await client.post('/api/v1/reports', json={**REPORT, 'reporter_id': 'x'})).status_code == 422
    monkeypatch.setattr(community, 'REPORTS_PER_HOUR', 1)
    await file_report(client, act_as, estate['resident'])
    assert (await client.post('/api/v1/reports', json=REPORT)).status_code == 429


@pytest.mark.asyncio
async def test_announcement_targets_audience_inbox_and_push(client, db_session, estate, act_as, monkeypatch):
    push = AsyncMock(side_effect=lambda tokens, *args, **kwargs: [PushResult(t, PushStatus.SENT) for t in tokens])
    monkeypatch.setattr(community.fcm_service, 'send_to_tokens', push)
    act_as(estate['caretaker'])
    response = await client.post('/api/v1/announcements', json={'title': 'Water outage', 'body': 'Water is off 9–11am.',
                                                                  'category': 'maintenance', 'audience': 'resident'})
    assert response.status_code == 201, response.text
    item = response.json()
    assert item['recipient_count'] == 2 and item['push_sent'] == 1 and item['push_failed'] == 0
    assert push.call_args.args[0] == ['resident-phone']
    assert push.call_args.kwargs['high_priority'] is False
    assert push.call_args.args[3]['app_id'] == 'COMM-ONE'
    recipients = set((await db_session.execute(select(AppNotification.user_id).where(
        AppNotification.event_key == f"announcement:{item['id']}"))).scalars().all())
    assert recipients == {estate['resident'].id, estate['neighbour'].id}
    history = (await client.get('/api/v1/announcements')).json()
    assert [a['id'] for a in history['announcements']] == [item['id']]


@pytest.mark.parametrize(('category', 'audience', 'expected'), [
    ('alert', 'all', {'resident', 'neighbour', 'host', 'guard', 'independent'}),
    ('security', 'security', {'guard'}),
    ('maintenance', 'landlord', {'host'}),
])
@pytest.mark.asyncio
async def test_announcement_deep_link_is_recipient_scoped(
    client, estate, act_as, monkeypatch, category, audience, expected,
):
    push = AsyncMock(return_value=[])
    monkeypatch.setattr(community.fcm_service, 'send_to_tokens', push)
    act_as(estate['caretaker'])
    response = await client.post('/api/v1/announcements', json={
        'title': 'Estate notice', 'body': 'Check the estate notice.',
        'category': category, 'audience': audience,
    })
    assert response.status_code == 201, response.text
    announcement = response.json()
    assert announcement['recipient_count'] == len(expected)
    assert push.call_args.kwargs['high_priority'] == (category in {'alert', 'security'})
    payload = community.fcm_service._build_message('device', 'Title', 'Body', push.call_args.args[3],
                                                  push.call_args.kwargs['high_priority'])['message']
    assert payload['android']['priority'] == ('NORMAL' if category == 'maintenance' else 'HIGH')
    assert payload['apns']['headers']['apns-priority'] == ('5' if category == 'maintenance' else '10')
    for role, user in estate.items():
        act_as(user)
        detail = await client.get(f"/api/v1/notifications/announcements/{announcement['id']}")
        assert detail.status_code == (200 if role in expected else 404)
        if role in expected:
            assert detail.json()['data']['category'] == category


@pytest.mark.asyncio
async def test_announcements_are_caretaker_only(client, estate, act_as):
    act_as(estate['resident'])
    assert (await client.post('/api/v1/announcements', json={'title': 'Hello', 'body': 'Hi all'})).status_code == 403
    assert (await client.get('/api/v1/announcements')).status_code == 403


INQUIRY = {'contact_name': 'Ada Caretaker', 'contact_email': 'ada@community.example', 'phone': '+234 800 000 0000',
           'homes': 240, 'message': 'We need gate access that keeps working offline.'}


@pytest.mark.asyncio
async def test_sales_inquiry_reaches_owner_console(client, estate, act_as, monkeypatch):
    from app.dependencies import get_current_saas_admin
    act_as(estate['resident'])
    assert (await client.post('/api/v1/sales-inquiries', json=INQUIRY)).status_code == 403
    act_as(estate['caretaker'])
    assert (await client.post('/api/v1/sales-inquiries', json={**INQUIRY, 'contact_email': 'nope'})).status_code == 422
    created = await client.post('/api/v1/sales-inquiries', json=INQUIRY)
    assert created.status_code == 201, created.text
    inquiry = created.json()
    assert inquiry['status'] == 'new' and inquiry['app_id'] == 'COMM-ONE' and inquiry['tier'] == 'enterprise'
    monkeypatch.setattr(community, 'SALES_INQUIRIES_PER_DAY', 1)
    assert (await client.post('/api/v1/sales-inquiries', json=INQUIRY)).status_code == 429
    # Owner-only endpoints.
    assert (await client.get('/api/v1/admin/sales-inquiries')).status_code in (401, 403)
    app.dependency_overrides[get_current_saas_admin] = lambda: estate['outsider']
    try:
        listed = (await client.get('/api/v1/admin/sales-inquiries?status=new')).json()
        assert [i['id'] for i in listed['inquiries']] == [inquiry['id']] and listed['counts']['new'] == 1
        assert listed['inquiries'][0]['contact_email'] == 'ada@community.example'
        updated = await client.patch(f"/api/v1/admin/sales-inquiries/{inquiry['id']}", json={'status': 'contacted', 'owner_note': 'Call booked.'})
        assert updated.status_code == 200 and updated.json()['status'] == 'contacted'
    finally:
        app.dependency_overrides.pop(get_current_saas_admin, None)
