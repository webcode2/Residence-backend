"""Caretakers register gate officers; officers must set their own password before working."""
import uuid
from unittest.mock import AsyncMock

import pytest

from app.models.user import User, UserRole
from app.services.auth import create_access_token, get_password_hash
from tests.test_onboarding import onboarding_estate  # noqa: F401  (fixture)

WELCOME = 'app.services.notifications.notification_service.send_staff_welcome_email'


def officer_body():
    return {'full_name': 'Officer Ada', 'email': f'Officer.{uuid.uuid4().hex[:6]}@Example.com', 'phone': '+2348011111111'}


async def sign_in(client, app_id, email, password):
    response = await client.post('/api/v1/auth/login', headers={'X-App-Id': app_id},
                                 json={'email': email, 'password': password})
    assert response.status_code == 200, response.text
    return {'X-App-Id': app_id, 'Authorization': 'Bearer ' + response.json()['token']['access_token']}


async def test_officer_lifecycle(client, onboarding_estate, monkeypatch):
    app_id, caretaker = onboarding_estate
    welcome = AsyncMock(return_value=True)
    monkeypatch.setattr(WELCOME, welcome)

    created = await client.post('/api/v1/security-staff', json=officer_body(), headers=caretaker)
    assert created.status_code == 201, created.text
    officer = created.json()
    assert officer['status'] == 'awaiting_first_sign_in' and officer['email'] == officer['email'].lower()
    temp = officer['temporary_password']
    # The worker-bound email carries the same temporary password (encrypted while queued).
    assert welcome.await_args.args[0] == officer['email'] and welcome.await_args.args[1] == temp

    # Signed in with the temporary password, the officer cannot use the app yet.
    headers = await sign_in(client, app_id, officer['email'], temp)
    blocked = await client.get('/api/v1/tokens/visitor/active', headers=headers)
    assert blocked.status_code == 403 and 'Password change required' in blocked.text

    changed = await client.post('/api/v1/auth/change-password', headers=headers,
                                json={'current_password': temp, 'new_password': 'officer-own-password-1'})
    assert changed.status_code == 200, changed.text
    headers = {**headers, 'Authorization': 'Bearer ' + changed.json()['token']['access_token']}
    assert (await client.get('/api/v1/tokens/visitor/active', headers=headers)).status_code == 200

    listed = (await client.get('/api/v1/security-staff', headers=caretaker)).json()
    assert [o['status'] for o in listed if o['id'] == officer['id']] == ['active']

    # Suspension takes effect on the officer's next request.
    suspended = await client.patch(f"/api/v1/users/{officer['id']}/status", headers=caretaker, json={'is_revoked': True})
    assert suspended.status_code == 200, suspended.text
    assert (await client.get('/api/v1/tokens/visitor/active', headers=headers)).status_code == 401
    assert (await client.post(f"/api/v1/security-staff/{officer['id']}/reset-password", headers=caretaker)).status_code == 409

    await client.patch(f"/api/v1/users/{officer['id']}/status", headers=caretaker, json={'is_revoked': False})
    reset = await client.post(f"/api/v1/security-staff/{officer['id']}/reset-password", headers=caretaker)
    assert reset.status_code == 200 and reset.json()['status'] == 'awaiting_first_sign_in'
    await sign_in(client, app_id, officer['email'], reset.json()['temporary_password'])


async def test_only_caretakers_register_officers_and_emails_are_unique(client, db_session, onboarding_estate, monkeypatch):
    app_id, caretaker = onboarding_estate
    monkeypatch.setattr(WELCOME, AsyncMock(return_value=True))
    resident = User(email=uuid.uuid4().hex + '@example.com', app_id=app_id, roles=[UserRole.RESIDENT],
                    hashed_password=get_password_hash('resident-password'))
    db_session.add(resident)
    await db_session.commit()
    resident_headers = {'X-App-Id': app_id, 'Authorization': 'Bearer ' + create_access_token({'sub': resident.email, 'app_id': app_id})}
    assert (await client.post('/api/v1/security-staff', json=officer_body(), headers=resident_headers)).status_code == 403
    assert (await client.get('/api/v1/security-staff', headers=resident_headers)).status_code == 403

    body = officer_body()
    assert (await client.post('/api/v1/security-staff', json=body, headers=caretaker)).status_code == 201
    duplicate = await client.post('/api/v1/security-staff', json={**body, 'email': body['email'].upper()}, headers=caretaker)
    assert duplicate.status_code == 409
    taken = await client.post('/api/v1/security-staff', json={**officer_body(), 'email': resident.email}, headers=caretaker)
    assert taken.status_code == 409
