import uuid
from datetime import date, timedelta
import pytest
from app.models.user import User, UserRole
from app.models.estate import Estate, Subscription
from app.services.auth import get_password_hash, create_access_token

@pytest.fixture
async def accounts(db_session):
    app_id = 'GUARD-' + uuid.uuid4().hex[:8]
    estate = Estate(name='Guard policy test', app_id=app_id, is_active=True)
    db_session.add(estate)
    await db_session.flush()
    db_session.add(Subscription(estate_id=estate.id, status='active', expiry_date=date.today()+timedelta(days=10), tier='standard', rfid_enabled=True))
    users = {}
    for role in [UserRole.RESIDENT, UserRole.LANDLORD, UserRole.CARETAKER, UserRole.SECURITY]:
        user = User(email=uuid.uuid4().hex+'@example.com', hashed_password=get_password_hash('test-password'), roles=[role], app_id=app_id, rfid_tag=uuid.uuid4().hex)
        db_session.add(user)
        users[role] = user
    await db_session.commit()
    def headers(role):
        user = users[role]
        return {'X-App-Id':app_id, 'Authorization':'Bearer '+create_access_token({'sub':user.email,'app_id':app_id,'roles':[role.value]})}
    return app_id, users, headers

@pytest.mark.asyncio
async def test_only_guards_can_verify(client, accounts):
    app_id, users, headers = accounts
    paths = ['/api/v1/verify/rfid/'+users[UserRole.RESIDENT].rfid_tag, '/api/v1/verify/token/INVALID', '/api/v1/verify/book-out/INVALID']
    for path in paths:
        assert (await client.get(path, headers={'X-App-Id':app_id})).status_code == 401
        for role in [UserRole.RESIDENT, UserRole.LANDLORD, UserRole.CARETAKER]:
            assert (await client.get(path, headers=headers(role))).status_code == 403
    result = await client.get(paths[0], headers=headers(UserRole.SECURITY))
    assert result.status_code == 200, result.text
    foreign = headers(UserRole.SECURITY)
    foreign['X-App-Id'] = 'FOREIGN-ESTATE'
    assert (await client.get(paths[0], headers=foreign)).status_code in (401,403)
    for role in [UserRole.RESIDENT, UserRole.CARETAKER]:
        assert (await client.post('/api/v1/tokens/visitor/'+str(uuid.uuid4())+'/checkout', headers=headers(role))).status_code == 403

@pytest.mark.asyncio
async def test_resident_cannot_bypass_guard_policy(client, accounts):
    _, users, headers = accounts
    resident = users[UserRole.RESIDENT]
    caretaker = users[UserRole.CARETAKER]
    for payload in [{'roles':['security']}, {'roles':['caretaker']}, {'rfid_tag':'changed'}]:
        assert (await client.patch('/api/v1/users/'+str(resident.id), json=payload, headers=headers(UserRole.RESIDENT))).status_code == 403
    assert (await client.patch('/api/v1/users/'+str(caretaker.id), json={'password':'replacement'}, headers=headers(UserRole.RESIDENT))).status_code == 403
    assert (await client.get('/api/v1/users/'+str(caretaker.id), headers=headers(UserRole.RESIDENT))).status_code == 403
    assert (await client.patch('/api/v1/users/'+str(resident.id), json={'full_name':'Updated name'}, headers=headers(UserRole.RESIDENT))).status_code == 200
    assert (await client.patch('/api/v1/users/'+str(resident.id), json={'house_number':'42'}, headers=headers(UserRole.CARETAKER))).status_code == 200
    assert (await client.patch('/api/v1/users/'+str(resident.id), json={'roles':['saas_owner']}, headers=headers(UserRole.CARETAKER))).status_code == 403

@pytest.mark.asyncio
async def test_directory_password_reset_is_durable_and_requires_change(client, db_session, accounts, monkeypatch):
    from unittest.mock import AsyncMock
    from app.models.base import utc_now
    from app.models.onboarding import AccountPasswordResetJob
    from app.services import account_password_resets
    from app.services.landlords import cipher

    app_id, users, headers = accounts
    host = users[UserRole.LANDLORD]
    path = f'/api/v1/users/{host.id}'
    caretaker = headers(UserRole.CARETAKER)
    send = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(account_password_resets.notification_service, 'send_staff_welcome_email', send)

    assert (await client.patch(path, json={'reset_password': True},
                               headers=headers(UserRole.RESIDENT))).status_code == 403
    assert (await client.patch(path, json={'password': 'chosen-by-admin'},
                               headers=caretaker)).status_code == 422
    changed = await client.patch(path, json={'reset_password': True, 'phone': '+2348000000000'},
                                 headers=caretaker)
    assert changed.status_code == 200, changed.text
    assert changed.json()['must_change_password'] is False
    assert changed.json()['phone'] == '+2348000000000'
    assert 'temporary_password' not in changed.json()
    queued = changed.json()['password_reset']
    assert queued['status'] == 'queued'
    repeated = await client.patch(path, json={'reset_password': True}, headers=caretaker)
    assert repeated.json()['password_reset']['id'] == queued['id']
    send.assert_not_awaited()

    job_id = uuid.UUID(queued['id'])
    job = await db_session.get(AccountPasswordResetJob, job_id)
    temporary = cipher().decrypt(job.password_ciphertext.encode()).decode()
    status = f'/api/v1/users/password-resets/{job_id}'
    assert (await client.get(status, headers=caretaker)).json()['status'] == 'queued'
    overview = await client.get('/api/v1/dashboard/overview', headers=caretaker)
    assert overview.status_code == 200, overview.text
    assert overview.json()['pending_password_resets'] == 1

    assert (await client.get(status, headers=headers(UserRole.RESIDENT))).status_code == 403
    assert await account_password_resets.process_one(db_session) is True
    assert (await client.get(status, headers=caretaker)).json()['status'] == 'retrying'
    assert job.password_ciphertext and job.next_attempt_at > utc_now()

    login_path = '/api/v1/auth/login'
    login_headers = {'X-App-Id': app_id}
    async def sign_in(password):
        return await client.post(login_path, json={'email': host.email, 'password': password},
                                 headers=login_headers)

    assert (await sign_in('test-password')).status_code == 200
    assert (await sign_in(temporary)).status_code == 401
    job.next_attempt_at = utc_now()
    await db_session.commit()
    assert await account_password_resets.process_one(db_session) is True
    assert (await client.get(status, headers=caretaker)).json()['status'] == 'sent'
    overview = await client.get('/api/v1/dashboard/overview', headers=caretaker)
    assert overview.json()['pending_password_resets'] == 0

    assert job.password_ciphertext is None
    assert send.call_args_list[0].kwargs['idempotency_key'] == send.call_args_list[1].kwargs['idempotency_key']
    assert (await sign_in('test-password')).status_code == 401
    login = await sign_in(temporary)
    assert login.status_code == 200, login.text
    restricted = {'X-App-Id': app_id, 'Authorization': 'Bearer '+login.json()['token']['access_token']}
    assert (await client.get('/api/v1/users/me/profile', headers=restricted)).status_code == 403
    complete = await client.post('/api/v1/auth/change-password', headers=restricted,
                                 json={'current_password': temporary, 'new_password': 'private-new-password'})
    assert complete.status_code == 200, complete.text
    assert complete.json()['user']['must_change_password'] is False

@pytest.mark.asyncio
async def test_directory_removal_archives_and_suspends_account(client, accounts):
    app_id, users, headers = accounts
    resident = users[UserRole.RESIDENT]
    caretaker = headers(UserRole.CARETAKER)
    path = f'/api/v1/users/{resident.id}/archive'
    original = headers(UserRole.RESIDENT)

    denied = await client.patch(path, headers=original)
    assert denied.status_code == 403
    archived = await client.patch(path, headers=caretaker)
    assert archived.status_code == 200, archived.text

    directory = await client.get('/api/v1/landlords/', headers=caretaker)
    assert directory.status_code == 200, directory.text
    assert str(resident.id) not in {row['id'] for row in directory.json()}
    archive = await client.get('/api/v1/users/archive', headers=caretaker)
    assert archive.status_code == 200, archive.text
    assert archive.json()[0]['id'] == str(resident.id)
    initial_date = archive.json()[0]['archived_at']
    assert (await client.patch(path, headers=caretaker)).status_code == 200
    assert (await client.get('/api/v1/users/archive', headers=caretaker)).json()[0]['archived_at'] == initial_date
    assert (await client.get('/api/v1/users/me/profile', headers=original)).status_code == 401

@pytest.mark.asyncio
async def test_archive_hold_is_caretaker_only_and_closes_after_thirty_days(client, db_session, accounts):
    from app.models.base import utc_now

    _, users, headers = accounts
    caretaker = headers(UserRole.CARETAKER)
    resident = users[UserRole.RESIDENT]
    host = users[UserRole.LANDLORD]
    for account in (resident, host):
        assert (await client.patch(f'/api/v1/users/{account.id}/archive',
                                   headers=caretaker)).status_code == 200
    host.archived_at = utc_now() - timedelta(days=30)
    await db_session.commit()

    path = f'/api/v1/users/{resident.id}/archive/hold'
    assert (await client.patch(path)).status_code == 401
    assert (await client.patch(f'/api/v1/users/{host.id}/archive/hold',
                               headers=caretaker)).status_code == 409
    assert (await client.patch(path, headers=caretaker)).status_code == 200
    assert (await client.patch(path, headers=caretaker)).status_code == 200
    response = await client.get('/api/v1/users/archive', headers=caretaker)
    rows = {row['id']: row for row in response.json()}
    assert rows[str(resident.id)]['hold_at'] is not None
    assert rows[str(host.id)]['hold_at'] is None
    assert (await client.get('/api/v1/users/me/profile',
                             headers=headers(UserRole.RESIDENT))).status_code == 401

@pytest.mark.asyncio
async def test_reinstatement_and_same_email_registration_are_separate(client, accounts):
    app_id, users, headers = accounts
    caretaker = headers(UserRole.CARETAKER)
    resident = users[UserRole.RESIDENT]
    original_email = resident.email
    original_headers = headers(UserRole.RESIDENT)
    path = f'/api/v1/users/{resident.id}/archive'

    assert (await client.patch(path, headers=caretaker)).status_code == 200
    assert (await client.patch(path + '/reinstate', headers=caretaker)).status_code == 200
    active = (await client.get('/api/v1/landlords/', headers=caretaker)).json()
    assert any(row['id'] == str(resident.id) for row in active)
    assert (await client.get('/api/v1/users/archive', headers=caretaker)).json() == []
    assert (await client.patch(path, headers=caretaker)).status_code == 200
    assert (await client.patch(path + '/hold', headers=caretaker)).status_code == 200
    retained = (await client.get('/api/v1/users/archive', headers=caretaker)).json()[0]
    assert retained['email'] == original_email and retained['hold_at']

    active = (await client.get('/api/v1/landlords/', headers=caretaker)).json()
    host = next(row for row in active if row['id'] == str(users[UserRole.LANDLORD].id))
    registered = await client.post('/api/v1/users/register', params={'registration_code': host['registration_token']}, json={
        'email': original_email, 'password': 'new-private-password',
        'full_name': 'Returning resident',
    }, headers={'X-App-Id': app_id})
    assert registered.status_code == 200, registered.text
    assert registered.json()['id'] != str(resident.id)
    assert (await client.get('/api/v1/users/me/profile', headers=original_headers)).status_code == 401
    assert (await client.patch(path + '/reinstate', headers=caretaker)).status_code == 409
    active = (await client.get('/api/v1/landlords/', headers=caretaker)).json()
    host = next(row for row in active if row['id'] == str(users[UserRole.LANDLORD].id))
    assert any(row['email'] == original_email and row['returning_user'] for row in host['occupants'])
    metrics = await client.get('/api/v1/dashboard/overview', headers=caretaker)
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()['returning_users'] == 1
    assert metrics.json()['archived_accounts'] == 1
    assert metrics.json()['retained_accounts'] == 1


@pytest.mark.asyncio
async def test_archive_purge_removes_due_records_and_keeps_holds(client, db_session, accounts):
    from app.models.access_log import AccessLog
    from app.models.base import utc_now
    from app.models.token import VisitorToken
    from app.services.account_archive import purge_one

    app_id, users, headers = accounts
    resident, host = users[UserRole.RESIDENT], users[UserRole.LANDLORD]
    resident_id, host_id = resident.id, host.id
    caretaker = headers(UserRole.CARETAKER)
    for person in (resident, host):
        assert (await client.patch(f'/api/v1/users/{person.id}/archive',
                                   headers=caretaker)).status_code == 200
    assert (await client.patch(f'/api/v1/users/{host_id}/archive/hold',
                               headers=caretaker)).status_code == 200
    log = AccessLog(app_id=app_id, resident_id=resident_id, event_type='entry',
                    identifier='fixture', status='authorized')
    token = VisitorToken(app_id=app_id, resident_id=resident_id, code='TEST01',
                         visitor_name='Visitor', expires_at=utc_now())
    db_session.add_all([log, token])
    await db_session.commit()
    log_id, token_id = log.id, token.id
    assert await purge_one(db_session) is False

    resident.archived_at = host.archived_at = utc_now() - timedelta(days=31)
    await db_session.commit()
    assert await purge_one(db_session) is True
    assert await db_session.get(User, resident_id) is None
    assert await db_session.get(AccessLog, log_id) is None
    assert await db_session.get(VisitorToken, token_id) is None
    assert await db_session.get(User, host_id) is not None
    assert await purge_one(db_session) is False


@pytest.mark.asyncio
async def test_public_reinstatement_request_emails_caretaker_once(client, accounts, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import notification_queue

    app_id, users, headers = accounts
    resident = users[UserRole.RESIDENT]
    caretaker = users[UserRole.CARETAKER]
    admin_headers = headers(UserRole.CARETAKER)
    send = AsyncMock()
    monkeypatch.setattr(notification_queue, 'dispatch', send)
    assert (await client.patch(f'/api/v1/users/{resident.id}/archive',
                               headers=admin_headers)).status_code == 200
    for email in (resident.email, resident.email.upper(), 'unrelated@example.com'):
        response = await client.post('/api/v1/auth/request-reinstatement',
                                     json={'email': email}, headers={'X-App-Id': app_id})
        assert response.status_code == 202, response.text
        assert 'If the account is eligible' in response.json()['message']
    send.assert_awaited_once()
    assert send.call_args.kwargs['email'] == caretaker.email
    rows = (await client.get('/api/v1/users/archive', headers=admin_headers)).json()
    assert rows[0]['reinstatement_requested'] is True
    assert (await client.get('/api/v1/users/me/profile',
                             headers=headers(UserRole.RESIDENT))).status_code == 401
