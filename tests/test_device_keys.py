"""Fingerprint / Face ID sign-in keys: residents only, one phone, revoked on account changes."""
import uuid

import pytest

from app.models.user import UserRole
from tests.test_guard_permissions import accounts  # noqa: F401  (fixture)


async def password_sign_in(client, app_id, user, password='test-password'):
    response = await client.post('/api/v1/auth/login', headers={'X-App-Id': app_id},
                                 json={'email': user.email, 'password': password})
    assert response.status_code == 200, response.text
    return {'X-App-Id': app_id, 'Authorization': 'Bearer ' + response.json()['token']['access_token']}


async def device_login(client, app_id, key):
    return await client.post('/api/v1/auth/device-login', headers={'X-App-Id': app_id},
                             json={'key_id': key['key_id'], 'secret': key['secret']})


@pytest.mark.asyncio
async def test_resident_signs_in_with_fingerprint_key(client, db_session, accounts):
    app_id, users, headers = accounts
    resident = users[UserRole.RESIDENT]
    session = await password_sign_in(client, app_id, resident)
    enrolled = await client.post('/api/v1/auth/device-keys', headers=session, json={'device_label': 'Pixel 8'})
    assert enrolled.status_code == 201, enrolled.text
    key = enrolled.json()

    signed_in = await device_login(client, app_id, key)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()['user']['email'] == resident.email
    fresh = {'X-App-Id': app_id, 'Authorization': 'Bearer ' + signed_in.json()['token']['access_token']}
    # One session per account: the fingerprint sign-in replaced the password session.
    assert (await client.get('/api/v1/users/me/profile', headers=session)).status_code == 401
    assert (await client.get('/api/v1/users/me/profile', headers=fresh)).status_code == 200

    wrong = await device_login(client, app_id, {**key, 'secret': key['secret'][:-2] + 'xx'})
    assert wrong.status_code == 401 and wrong.headers['x-device-key'] == 'invalid'
    other_estate = await client.post('/api/v1/auth/device-login', headers={'X-App-Id': 'OTHER-' + app_id},
                                     json={'key_id': key['key_id'], 'secret': key['secret']})
    assert other_estate.status_code in (401, 403, 404)

    # Enrolling again (new phone) retires the old key.
    second = (await client.post('/api/v1/auth/device-keys', headers=fresh, json={})).json()
    assert (await device_login(client, app_id, key)).status_code == 401
    signed_in = await device_login(client, app_id, second)
    assert signed_in.status_code == 200
    fresh = {'X-App-Id': app_id, 'Authorization': 'Bearer ' + signed_in.json()['token']['access_token']}

    # Turning it off on the phone revokes the key.
    assert (await client.delete(f"/api/v1/auth/device-keys/{second['key_id']}", headers=fresh)).status_code == 204
    assert (await device_login(client, app_id, second)).status_code == 401


@pytest.mark.asyncio
async def test_key_stops_working_after_password_change_or_suspension(client, db_session, accounts):
    app_id, users, headers = accounts
    resident = users[UserRole.LANDLORD]
    session = await password_sign_in(client, app_id, resident)
    key = (await client.post('/api/v1/auth/device-keys', headers=session, json={})).json()
    changed = await client.post('/api/v1/auth/change-password', headers=session,
                                json={'current_password': 'test-password', 'new_password': 'brand-new-password'})
    assert changed.status_code == 200, changed.text
    assert (await device_login(client, app_id, key)).status_code == 401

    session = await password_sign_in(client, app_id, resident, 'brand-new-password')
    key = (await client.post('/api/v1/auth/device-keys', headers=session, json={})).json()
    resident.is_revoked = True
    await db_session.commit()
    assert (await device_login(client, app_id, key)).status_code == 401
    resident.is_revoked = False
    await db_session.commit()
    # A key revoked because of suspension stays revoked after reinstatement.
    assert (await device_login(client, app_id, key)).status_code == 401


@pytest.mark.asyncio
async def test_security_officers_cannot_enrol(client, accounts):
    app_id, users, headers = accounts
    officer = await password_sign_in(client, app_id, users[UserRole.SECURITY])
    assert (await client.post('/api/v1/auth/device-keys', headers=officer, json={})).status_code == 403
    assert (await client.post('/api/v1/auth/device-keys', headers={'X-App-Id': app_id}, json={})).status_code == 401
    assert (await client.post('/api/v1/auth/device-login', headers={'X-App-Id': app_id},
                              json={'key_id': str(uuid.uuid4()), 'secret': 'x' * 40})).status_code == 401
