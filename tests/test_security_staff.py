"""Caretakers register gate officers; officers must set their own password before working."""
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.admin_audit import AdminAuditLog

from app.models.user import User, UserRole
from app.services import account_password_resets
from app.services.auth import create_access_token, get_password_hash
from tests.test_onboarding import onboarding_estate  # noqa: F401  (fixture)

WELCOME = 'app.services.notifications.notification_service.send_credential_email'


def officer_body():
    return {'full_name': 'Officer Ada', 'email': f'Officer.{uuid.uuid4().hex[:6]}@Example.com', 'phone': '+2348011111111'}


async def login(client, app_id, email, password):
    return await client.post('/api/v1/auth/login', headers={'X-App-Id': app_id},
                             json={'email': email, 'password': password})


async def sign_in(client, app_id, email, password):
    response = await login(client, app_id, email, password)
    assert response.status_code == 200, response.text
    return {'X-App-Id': app_id, 'Authorization': 'Bearer ' + response.json()['token']['access_token']}


async def send_due_emails(db_session):
    """What the worker does: deliver every due credential email."""
    while await account_password_resets.process_one(db_session):
        pass


async def test_officer_lifecycle(client, db_session, onboarding_estate, monkeypatch):
    app_id, caretaker = onboarding_estate
    welcome = AsyncMock(return_value=True)
    monkeypatch.setattr(WELCOME, welcome)

    created = await client.post('/api/v1/security-staff', json=officer_body(), headers=caretaker)
    assert created.status_code == 201, created.text
    officer = created.json()
    assert officer['status'] == 'awaiting_first_sign_in' and officer['email'] == officer['email'].lower()
    assert officer['email_status'] == 'queued'
    temp = officer['temporary_password']
    # The password only works once the worker's email is accepted.
    welcome.assert_not_awaited()
    assert (await login(client, app_id, officer['email'], temp)).status_code == 401
    await send_due_emails(db_session)
    assert welcome.await_args.args[:3] == ('staff_welcome', officer['email'], temp)
    listed = (await client.get('/api/v1/security-staff', headers=caretaker)).json()
    assert [o['email_status'] for o in listed if o['id'] == officer['id']] == ['sent']

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
    path = f"/api/v1/users/{officer['id']}/status"
    for invalid in ({}, {'reason': '  '}, {'reason': 'x' * 501}):
        response = await client.patch(path, headers=caretaker, json={'is_revoked': True, **invalid})
        assert response.status_code == 422, response.text
    assert (await client.get('/api/v1/tokens/visitor/active', headers=headers)).status_code == 200
    suspended = await client.patch(path, headers=caretaker, json={'is_revoked': True, 'reason': '  Duty review  '})
    assert suspended.status_code == 200, suspended.text
    entry = (await db_session.execute(select(AdminAuditLog).where(
        AdminAuditLog.action == 'user.suspend', AdminAuditLog.target_id == officer['id'],
    ))).scalar_one()
    assert entry.changes['reason']['after'] == 'Duty review'
    assert (await client.get('/api/v1/tokens/visitor/active', headers=headers)).status_code == 401
    assert (await client.post(f"/api/v1/security-staff/{officer['id']}/reset-password", headers=caretaker)).status_code == 409

    await client.patch(f"/api/v1/users/{officer['id']}/status", headers=caretaker, json={'is_revoked': False})
    reset = await client.post(f"/api/v1/security-staff/{officer['id']}/reset-password", headers=caretaker)
    assert reset.status_code == 200 and reset.json()['email_status'] == 'queued'
    new_temp = reset.json()['temporary_password']
    # The officer's own password keeps working until the reset email is accepted.
    assert (await login(client, app_id, officer['email'], new_temp)).status_code == 401
    await sign_in(client, app_id, officer['email'], 'officer-own-password-1')
    await send_due_emails(db_session)
    assert welcome.await_args.args[:3] == ('reset', officer['email'], new_temp)
    assert (await login(client, app_id, officer['email'], 'officer-own-password-1')).status_code == 401
    await sign_in(client, app_id, officer['email'], new_temp)


async def test_officer_reset_survives_email_failures(client, db_session, onboarding_estate, monkeypatch):
    from app.models.base import utc_now
    from app.models.onboarding import AccountPasswordResetJob

    app_id, caretaker = onboarding_estate
    send = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(WELCOME, send)
    officer = (await client.post('/api/v1/security-staff', json=officer_body(), headers=caretaker)).json()
    temp = officer['temporary_password']
    # Resetting while the welcome email is still pending reuses the same password.
    again = await client.post(f"/api/v1/security-staff/{officer['id']}/reset-password", headers=caretaker)
    assert again.json()['temporary_password'] == temp

    await send_due_emails(db_session)  # provider refuses: job retries later, password not active
    job = (await db_session.execute(select(AccountPasswordResetJob).where(
        AccountPasswordResetJob.user_id == uuid.UUID(officer['id'])))).scalar_one()
    assert job.status == 'retrying' and job.kind == 'staff_welcome'
    assert (await login(client, app_id, officer['email'], temp)).status_code == 401
    listed = (await client.get('/api/v1/security-staff', headers=caretaker)).json()
    assert [o['email_status'] for o in listed if o['id'] == officer['id']] == ['retrying']

    job.next_attempt_at = utc_now()
    await db_session.commit()
    await send_due_emails(db_session)
    assert job.status == 'sent' and job.password_ciphertext is None
    await sign_in(client, app_id, officer['email'], temp)


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


async def test_estate_sign_in_ignores_email_capitalisation(client, db_session, onboarding_estate, monkeypatch):
    app_id, caretaker = onboarding_estate
    monkeypatch.setattr(WELCOME, AsyncMock(return_value=True))
    officer = (await client.post('/api/v1/security-staff', json=officer_body(), headers=caretaker)).json()
    await send_due_emails(db_session)
    typed = officer['email'][0].upper() + officer['email'][1:]  # what a phone keyboard sends
    response = await client.post('/api/v1/auth/login', headers={'X-App-Id': app_id},
                                 json={'email': '  ' + typed + ' ', 'password': officer['temporary_password']})
    assert response.status_code == 200, response.text


async def test_sign_in_never_guesses_between_accounts_differing_only_by_case(client, db_session, onboarding_estate):
    app_id, _ = onboarding_estate
    local = 'q' + uuid.uuid4().hex[:8]  # starts with a letter so capitalising changes it
    for email, password in ((f'{local}@example.com', 'lower-password-1'), (f'{local.upper()}@example.com', 'upper-password-1')):
        db_session.add(User(email=email, app_id=app_id, roles=[UserRole.RESIDENT], hashed_password=get_password_hash(password)))
    await db_session.commit()
    login = lambda email, pw: client.post('/api/v1/auth/login', headers={'X-App-Id': app_id}, json={'email': email, 'password': pw})
    assert (await login(f'{local}@example.com', 'lower-password-1')).status_code == 200         # exact match wins
    assert (await login(f'{local.upper()}@example.com', 'upper-password-1')).status_code == 200
    assert (await login(f'{local.capitalize()}@example.com', 'lower-password-1')).status_code == 401  # ambiguous


async def test_caretaker_corrects_officer_details(client, db_session, onboarding_estate, monkeypatch):
    from app.models.onboarding import AccountPasswordResetJob

    app_id, caretaker = onboarding_estate
    welcome = AsyncMock(return_value=True)
    monkeypatch.setattr(WELCOME, welcome)
    body = {**officer_body(), 'email': f'peter.{uuid.uuid4().hex[:6]}@gmaill.com', 'home_address': ' 4 Gate Rd '}
    officer = (await client.post('/api/v1/security-staff', json=body, headers=caretaker)).json()
    assert officer['home_address'] == '4 Gate Rd'
    path = f"/api/v1/security-staff/{officer['id']}"

    # The welcome went to a mistyped address and was never used: fixing it sends a new one.
    fixed_email = officer['email'].replace('@gmaill.com', '@gmail.com')
    edited = await client.patch(path, headers=caretaker, json={
        'email': fixed_email.upper(), 'full_name': 'Peter James', 'phone': '08128991543',
        'staff_number': 'SEC-014', 'alternate_phone': '+2348022222222',
        'emergency_contact_name': 'Mary James', 'emergency_contact_phone': '+2348033333333',
        'notes': 'Night shift', 'home_address': ''})
    assert edited.status_code == 200, edited.text
    data = edited.json()
    assert data['email'] == fixed_email and data['full_name'] == 'Peter James'
    assert data['staff_number'] == 'SEC-014' and data['home_address'] is None
    assert data['temporary_password'] and data['email_status'] == 'queued'
    await send_due_emails(db_session)
    assert welcome.await_args.args[:3] == ('staff_welcome', fixed_email, data['temporary_password'])
    jobs = (await db_session.execute(select(AccountPasswordResetJob.email, AccountPasswordResetJob.status).where(
        AccountPasswordResetJob.user_id == uuid.UUID(officer['id'])))).all()
    # The unsent welcome to the mistyped address is cancelled, never delivered.
    assert sorted(jobs) == sorted([(officer['email'], 'cancelled'), (fixed_email, 'sent')])
    assert welcome.await_count == 1
    headers = await sign_in(client, app_id, fixed_email, data['temporary_password'])

    listed = (await client.get('/api/v1/security-staff', headers=caretaker)).json()
    row = next(o for o in listed if o['id'] == officer['id'])
    assert row['emergency_contact_name'] == 'Mary James' and row['notes'] == 'Night shift'
    audit = (await db_session.execute(select(AdminAuditLog).where(
        AdminAuditLog.action == 'security_staff.update', AdminAuditLog.target_id == officer['id']))).scalar_one()
    assert audit.changes['email'] == {'before': officer['email'], 'after': fixed_email}

    # An officer who already uses the app keeps their password but signs in with the new email.
    changed = await client.post('/api/v1/auth/change-password', headers=headers,
                                json={'current_password': data['temporary_password'], 'new_password': 'officer-own-password-1'})
    headers = {**headers, 'Authorization': 'Bearer ' + changed.json()['token']['access_token']}
    newest = f'pj.{uuid.uuid4().hex[:6]}@example.com'
    moved = await client.patch(path, headers=caretaker, json={'email': newest})
    assert moved.status_code == 200 and moved.json()['temporary_password'] is None
    assert (await client.get('/api/v1/tokens/visitor/active', headers=headers)).status_code == 401
    assert (await login(client, app_id, fixed_email, 'officer-own-password-1')).status_code == 401

    other = (await client.post('/api/v1/security-staff', json=officer_body(), headers=caretaker)).json()
    taken = await client.patch(path, headers=caretaker, json={'email': other['email'].upper()})
    assert taken.status_code == 409
    assert (await client.patch(path, headers=caretaker, json={'full_name': '  '})).status_code == 422
    assert (await client.patch(path, headers=caretaker, json={'email': None})).status_code == 422
    assert (await client.patch(f'/api/v1/security-staff/{uuid.uuid4()}', headers=caretaker,
                               json={'phone': '1'})).status_code == 404
    officer_headers = await sign_in(client, app_id, newest, 'officer-own-password-1')
    assert (await client.patch(path, headers=officer_headers, json={'phone': '1'})).status_code == 403
