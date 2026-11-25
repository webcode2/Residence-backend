"""Cache-aside reads (profile, visitor list) and worker-queued notifications."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.core.redis import (get_cached_user, get_cached_visitor_history, get_redis_client,
                            set_cached_user, set_token_live_status)
from app.models.estate import Estate, Subscription
from app.models.token import VisitorToken
from app.models.user import User, UserRole
from app.services import notification_queue, worker_wake
from app.services.auth import create_access_token, verify_password
from app.services.fcm import PushResult, PushStatus
from tests.test_onboarding import onboarding_estate, row  # noqa: F401  (fixture)


@pytest.fixture
async def resident(db_session):
    app_id = 'RP-' + uuid.uuid4().hex[:6].upper()
    estate = Estate(app_id=app_id, name='Cache estate', is_active=True)
    db_session.add(estate)
    await db_session.flush()
    db_session.add(Subscription(estate_id=estate.id, status='active', tier='standard',
                                expiry_date=(datetime.now(UTC) + timedelta(days=30)).date()))
    user = User(email=uuid.uuid4().hex + '@example.com', hashed_password='hash', roles=[UserRole.RESIDENT],
                app_id=app_id, phone='+2348000000001', full_name='Cache Resident')
    db_session.add(user)
    await db_session.commit()
    headers = {'Authorization': 'Bearer ' + create_access_token({'sub': user.email, 'app_id': app_id}),
               'X-App-Id': app_id}
    await get_redis_client().delete(f'residence:user:{app_id}:{user.email}',
                                    f'residence:visitor_history:{app_id}:{user.id}')
    return user, headers


@pytest.fixture
async def wake_configured(monkeypatch):
    monkeypatch.setattr(settings, 'WORKER_WAKE_URL', 'https://worker.example/internal/drain')
    monkeypatch.setattr(settings, 'WORKER_TASKS_QUEUE', 'projects/p/locations/us-east4/queues/q')
    monkeypatch.setattr(settings, 'WORKER_TASKS_INVOKER_SA', 'tasks@p.iam.gserviceaccount.com')
    created = AsyncMock()
    monkeypatch.setattr(worker_wake, '_create_task', created)
    client = get_redis_client()
    await client.delete(notification_queue.NOTIFY_QUEUE_KEY)
    await worker_wake.clear_wake_flag()
    yield created
    await client.delete(notification_queue.NOTIFY_QUEUE_KEY)
    await worker_wake.clear_wake_flag()


# --- Profile: Redis first, Postgres on miss, refreshed on every change -----------------

async def test_profile_is_served_from_cache_and_refreshed_after_update(client, db_session, resident):
    user, headers = resident
    first = await client.get('/api/v1/users/me/profile', headers=headers)
    assert first.status_code == 200 and first.json()['phone'] == '+2348000000001'
    cached = await get_cached_user(user.app_id, user.email)
    assert cached and cached['phone'] == '+2348000000001'

    # A direct DB change is not visible while the cache entry lives (served from Redis)...
    await db_session.execute(update(User).where(User.id == user.id).values(full_name='Changed In DB'))
    await db_session.commit()
    assert (await client.get('/api/v1/users/me/profile', headers=headers)).json()['full_name'] == 'Cache Resident'
    # ...but an update through the API invalidates it, and the next read reloads Postgres.
    patched = await client.patch('/api/v1/users/me/profile', headers=headers, json={'phone': '+2348000000002'})
    assert patched.status_code == 200, patched.text
    headers = {**headers, 'Authorization': 'Bearer ' + patched.json()['token']['access_token']}  # rotated on update
    fresh = (await client.get('/api/v1/users/me/profile', headers=headers)).json()
    assert fresh['phone'] == '+2348000000002' and fresh['full_name'] == 'Changed In DB'


async def test_old_cache_shape_is_treated_as_a_miss(client, resident):
    user, headers = resident
    await set_cached_user(user.app_id, user.email, {'id': str(user.id), 'email': user.email, 'app_id': user.app_id,
                                                     'roles': ['resident'], 'created_at': user.created_at.isoformat()})
    body = (await client.get('/api/v1/users/me/profile', headers=headers)).json()
    assert body['phone'] == '+2348000000001'  # reloaded from Postgres, not the incomplete entry
    assert (await get_cached_user(user.app_id, user.email))['v'] == 2


async def test_creating_an_account_clears_a_stale_cache_entry(client, onboarding_estate, monkeypatch):
    app_id, headers = onboarding_estate
    monkeypatch.setattr('app.services.landlords.notification_service.send_registration_email', AsyncMock(return_value=True))
    data = row()
    await set_cached_user(app_id, data['email'], {'v': 2, 'id': str(uuid.uuid4()), 'email': data['email'],
                                                  'app_id': app_id, 'roles': ['resident'], 'full_name': 'Old account'})
    assert (await client.post('/api/v1/landlords/', json=data, headers=headers)).status_code == 201
    assert await get_cached_user(app_id, data['email']) is None


# --- Visitor list: cache-aside with live status overlay --------------------------------

async def test_visitor_list_is_cached_invalidated_on_create_and_shows_live_status(client, db_session, resident):
    user, headers = resident
    first = (await client.post('/api/v1/tokens/visitor', json={'visitor_name': 'First guest'}, headers=headers)).json()
    listed = (await client.get('/api/v1/tokens/visitor/history', headers=headers)).json()
    assert [t['id'] for t in listed] == [first['id']]
    assert await get_cached_visitor_history(user.app_id, str(user.id)) is not None

    # Served from Redis: a direct DB rename is not seen until the entry is invalidated.
    await db_session.execute(update(VisitorToken).where(VisitorToken.id == uuid.UUID(first['id'])).values(visitor_name='Renamed'))
    await db_session.commit()
    assert (await client.get('/api/v1/tokens/visitor/history', headers=headers)).json()[0]['visitor_name'] == 'First guest'

    # Gate events still show immediately through the live-status overlay.
    await set_token_live_status(first['id'], 'checked_in', {'checked_in_at': datetime.now(UTC).replace(tzinfo=None).isoformat()})
    assert (await client.get('/api/v1/tokens/visitor/history', headers=headers)).json()[0]['status'] == 'checked_in'

    # Creating a token invalidates the cached list; the next read reloads Postgres.
    second = (await client.post('/api/v1/tokens/visitor', json={'visitor_name': 'Second guest'}, headers=headers)).json()
    names = {t['id']: t['visitor_name'] for t in (await client.get('/api/v1/tokens/visitor/history', headers=headers)).json()}
    assert names == {second['id']: 'Second guest', first['id']: 'Renamed'}


# --- Notifications: queued for the worker ----------------------------------------------

async def test_notifications_are_queued_and_the_worker_is_woken(wake_configured, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr('app.services.fcm.fcm_service.send_to_token', send)
    await notification_queue.dispatch('push', token='t1', title='Visitor arrival', body='At the gate', data={'a': 1})
    await notification_queue.dispatch('email_alert', email='r@example.com', title='Gate', message='Entry')
    send.assert_not_awaited()  # nothing is sent in the request
    queued = [json.loads(x) for x in await get_redis_client().lrange(notification_queue.NOTIFY_QUEUE_KEY, 0, -1)]
    assert [q['kind'] for q in queued] == ['push', 'email_alert']
    wake_configured.assert_awaited_once()  # one wake for the burst


async def test_worker_sends_and_retries_only_transient_failures(wake_configured, monkeypatch):
    results = {'ok': PushStatus.SENT, 'stale': PushStatus.UNREGISTERED, 'flaky': PushStatus.FAILED}
    send = AsyncMock(side_effect=lambda token, *a, **k: PushResult(token, results[token]))
    monkeypatch.setattr('app.services.fcm.fcm_service.send_to_token', send)
    for token in results:
        await notification_queue.dispatch('push', token=token, title='t', body='b')
    for _ in range(notification_queue.MAX_ATTEMPTS + 1):
        await notification_queue.drain_notification_queue()
    sent_tokens = [c.args[0] for c in send.await_args_list]
    assert sent_tokens.count('ok') == 1 and sent_tokens.count('stale') == 1
    assert sent_tokens.count('flaky') == notification_queue.MAX_ATTEMPTS
    assert await get_redis_client().llen(notification_queue.NOTIFY_QUEUE_KEY) == 0


async def test_chat_push_plan_survives_the_queue(wake_configured, monkeypatch):
    from app.services.chat import ChatDispatch, PushTarget
    sent = AsyncMock()
    monkeypatch.setattr('app.services.chat.send_push_notifications', sent)
    plan = ChatDispatch(app_id='A', thread_id='T', event={'x': 1}, title='New message', body='Hi',
                        push_targets=[PushTarget(user_id='u1', token='tok', notification_count=2)])
    await notification_queue.dispatch('chat_push', dispatch=plan)
    await notification_queue.drain_notification_queue()
    restored = sent.await_args.args[0]
    assert restored == plan


async def test_without_a_worker_notifications_still_send_in_process(monkeypatch):
    monkeypatch.setattr(settings, 'WORKER_WAKE_URL', '')
    send = AsyncMock(return_value=PushResult('t', PushStatus.SENT))
    monkeypatch.setattr('app.services.fcm.fcm_service.send_to_token', send)
    await notification_queue.dispatch('push', token='t', title='t', body='b')
    send.assert_awaited_once()  # sent before dispatch returns


# --- Resend welcome keeps the new password ---------------------------------------------

async def test_resend_welcome_password_works_when_a_job_already_exists(client, db_session, onboarding_estate, monkeypatch):
    app_id, headers = onboarding_estate
    monkeypatch.setattr('app.services.landlords.notification_service.send_registration_email', AsyncMock(return_value=True))
    created = (await client.post('/api/v1/landlords/', json=row(), headers=headers)).json()
    resent = await client.post(f"/api/v1/landlords/{created['id']}/resend-welcome", headers=headers)
    assert resent.status_code == 200, resent.text
    user = (await db_session.execute(select(User).where(User.id == uuid.UUID(created['id']))
                                     .execution_options(populate_existing=True))).scalar_one()
    assert verify_password(resent.json()['temporary_password'], user.hashed_password)
    assert user.must_change_password is True
