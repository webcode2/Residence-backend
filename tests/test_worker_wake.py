"""API queues work and wakes the worker; the worker drains it. Nothing runs inline."""
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker_app as worker_app
from app.core.config import settings
from app.core.redis import get_redis_client
from app.dependencies import get_current_user
from app.main import app
from app.models.access_log import AccessLog
from app.models.base import utc_now
from app.models.onboarding import LandlordOnboardingJob as Job
from app.models.sos import SOSDelivery
from app.services import sos, worker_wake
from app.services.access_logs import record_access_event
from app.services.fcm import PushResult, PushStatus
from tests.test_onboarding import onboarding_estate, row  # noqa: F401  (fixture)
from tests.test_sos import members  # noqa: F401  (fixture)

SEND = 'app.services.landlords.notification_service.send_registration_email'


@pytest.fixture
async def wake_configured(monkeypatch):
    monkeypatch.setattr(settings, 'WORKER_WAKE_URL', 'https://worker.example/internal/drain')
    monkeypatch.setattr(settings, 'WORKER_TASKS_QUEUE', 'projects/p/locations/us-east4/queues/q')
    monkeypatch.setattr(settings, 'WORKER_TASKS_INVOKER_SA', 'tasks@p.iam.gserviceaccount.com')
    created = AsyncMock()
    monkeypatch.setattr(worker_wake, '_create_task', created)
    await worker_wake.clear_wake_flag()
    yield created
    await worker_wake.clear_wake_flag()


@pytest.fixture
def worker_db(engine, setup_db, monkeypatch):
    """Point the worker's own sessions at the test database."""
    monkeypatch.setattr(worker_app, 'SessionLocal',
                        async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False))


async def test_wake_is_noop_when_not_configured(monkeypatch):
    created = AsyncMock()
    monkeypatch.setattr(worker_wake, '_create_task', created)
    monkeypatch.setattr(settings, 'WORKER_WAKE_URL', '')
    assert await worker_wake.wake_worker('test') is False
    created.assert_not_awaited()


async def test_burst_of_events_creates_one_wake(wake_configured):
    assert await worker_wake.wake_worker('a') is True
    assert await worker_wake.wake_worker('b') is False
    assert await worker_wake.wake_worker('c') is False
    assert wake_configured.await_count == 1
    await worker_wake.clear_wake_flag()  # worker finished draining
    assert await worker_wake.wake_worker('d') is True
    assert wake_configured.await_count == 2


async def test_scheduled_retry_wakes_bypass_the_burst_flag(wake_configured):
    assert await worker_wake.wake_worker('now') is True
    at = utc_now() + timedelta(minutes=5)
    assert await worker_wake.wake_worker('retry', at=at) is True
    assert wake_configured.await_args_list[-1].args == (at, 'retry')


async def test_failed_wake_never_breaks_the_request_and_frees_the_flag(wake_configured):
    wake_configured.side_effect = RuntimeError('tasks api down')
    assert await worker_wake.wake_worker('a') is False
    wake_configured.side_effect = None
    assert await worker_wake.wake_worker('b') is True


async def test_add_host_queues_email_and_responds_without_sending(client, onboarding_estate, wake_configured, monkeypatch):
    _, headers = onboarding_estate
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(SEND, send)
    response = await client.post('/api/v1/landlords/', json=row(), headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['email_status'] == 'queued' and body['email_sent'] is None and body['email_warning'] is None
    send.assert_not_awaited()
    wake_configured.assert_awaited_once()


async def test_raising_sos_wakes_worker_without_pushing_inline(client, members, wake_configured, monkeypatch):
    push = AsyncMock()
    monkeypatch.setattr(sos.fcm_service, 'send_to_token', push)
    app.dependency_overrides[get_current_user] = lambda: members[0]
    try:
        response = await client.post('/api/v1/sos', json={'request_id': str(uuid.uuid4())})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200, response.text
    push.assert_not_awaited()
    wake_configured.assert_awaited_once()


async def test_worker_drains_every_queue_and_schedules_retries(client, db_session, onboarding_estate, members,
                                                               wake_configured, worker_db, monkeypatch):
    _, headers = onboarding_estate
    # Queue: a welcome email, an SOS, and a gate access log on Redis.
    monkeypatch.setattr(SEND, AsyncMock(return_value=True))
    job_id = uuid.UUID((await client.post('/api/v1/landlords/', json=row(), headers=headers)).json()['job_id'])
    await sos.create(db_session, members[0], uuid.uuid4())
    identifier = 'GATE-' + uuid.uuid4().hex[:6]
    await record_access_event(db_session, app_id='WORKER-EST', event_type='visitor_entry',
                              identifier=identifier, status='granted')
    assert await get_redis_client().llen('residence:access_log:queue') >= 1

    push = AsyncMock(side_effect=lambda token, *args: PushResult(token, PushStatus.SENT))
    monkeypatch.setattr(sos.fcm_service, 'send_to_token', push)
    wake_configured.reset_mock()

    result = await worker_app.drain_all(budget_seconds=30)

    assert result['handled']['welcome_emails'] >= 1 and result['handled']['sos_pushes'] == 2
    assert result['handled']['access_logs'] >= 1
    assert push.await_count == 2
    fresh = dict(execution_options={'populate_existing': True})
    assert (await db_session.execute(select(Job.status).where(Job.id == job_id))).scalar_one() == 'sent'
    statuses = {d.status for d in (await db_session.execute(select(SOSDelivery), **fresh)).scalars()}
    assert statuses == {'awaiting_receipt'}
    assert (await db_session.execute(select(AccessLog).where(AccessLog.identifier == identifier))).scalars().all()
    # SOS receipts are due later, so the worker booked its own next wake.
    assert result['next_wake'] is not None
    assert any(call.args[1] == 'retry' for call in wake_configured.await_args_list)


async def test_queued_gate_log_for_deleted_resident_does_not_block_the_queue(db_session, worker_db):
    from app.core.redis import enqueue_access_log
    from app.services.access_logs import drain_access_log_queue
    client = get_redis_client()
    await client.delete('residence:access_log:queue')
    ghost, kept = str(uuid.uuid4()), 'GATE-' + uuid.uuid4().hex[:6]
    await enqueue_access_log({'app_id': 'WORKER-EST', 'event_type': 'visitor_entry', 'identifier': kept,
                              'status': 'granted', 'resident_id': ghost, 'created_at': utc_now().isoformat()})
    await enqueue_access_log({'app_id': 'WORKER-EST', 'event_type': 'visitor_entry', 'identifier': kept + '-2',
                              'status': 'granted', 'created_at': utc_now().isoformat()})
    assert await drain_access_log_queue(db_session) == 2
    assert await client.llen('residence:access_log:queue') == 0
    rows = (await db_session.execute(select(AccessLog).where(AccessLog.identifier.like(kept + '%')))).scalars().all()
    assert len(rows) == 2 and all(r.resident_id is None for r in rows)
