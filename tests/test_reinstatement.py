"""Reinstatement is only for suspended accounts, never active ones."""
import uuid
from unittest.mock import AsyncMock

from app.models.user import User, UserRole
from app.services.auth import get_password_hash
from tests.test_onboarding import onboarding_estate  # noqa: F401  (fixture)

PASSWORD = 'resident-password-1'


async def _resident(db_session, app_id, revoked=False):
    user = User(email=f'res-{uuid.uuid4().hex[:8]}@example.com', app_id=app_id, roles=[UserRole.RESIDENT],
                hashed_password=get_password_hash(PASSWORD), is_revoked=revoked)
    db_session.add(user)
    await db_session.commit()
    return user


def _login(client, app_id, email, password):
    return client.post('/api/v1/auth/login', headers={'X-App-Id': app_id}, json={'email': email, 'password': password})


async def test_only_a_suspended_account_with_the_right_password_is_marked(client, db_session, onboarding_estate):
    app_id, _ = onboarding_estate
    active = await _resident(db_session, app_id)
    suspended = await _resident(db_session, app_id, revoked=True)

    wrong = await _login(client, app_id, active.email, 'not-the-password')
    assert wrong.status_code == 401 and 'x-account-status' not in wrong.headers
    assert (await _login(client, app_id, active.email, PASSWORD)).status_code == 200

    guessed = await _login(client, app_id, suspended.email, 'not-the-password')
    assert guessed.status_code == 401 and 'x-account-status' not in guessed.headers  # nothing leaks
    marked = await _login(client, app_id, suspended.email, PASSWORD)
    assert marked.status_code == 401 and marked.headers.get('x-account-status') == 'suspended'


async def test_requests_reach_caretakers_only_for_suspended_accounts(client, db_session, onboarding_estate, monkeypatch):
    app_id, _ = onboarding_estate
    dispatch = AsyncMock()
    monkeypatch.setattr('app.services.account_archive.notification_queue.dispatch', dispatch)
    active = await _resident(db_session, app_id)
    suspended = await _resident(db_session, app_id, revoked=True)

    for user in (active, suspended):
        response = await client.post('/api/v1/auth/request-reinstatement', headers={'X-App-Id': app_id},
                                     json={'email': user.email})
        assert response.status_code == 202  # same reply either way
    notified = [c.kwargs['message'] for c in dispatch.await_args_list]
    assert len(notified) == 1 and suspended.email in notified[0] and active.email not in notified[0]
    assert 'Resident directory' in notified[0]
