import uuid
from datetime import date, timedelta
import pytest
from starlette.requests import Request
from app.models.user import User, UserRole
from app.models.estate import Estate, Subscription
from app.services.auth import get_password_hash, create_access_token
from app.api.v1.endpoints.verify import _get_client_ip

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

def test_forwarded_header_cannot_change_rate_limit_identity():
    request = Request({'type':'http','headers':[(b'x-forwarded-for',b'198.51.100.123')],'client':('203.0.113.1',1234),'method':'GET','path':'/'})
    assert _get_client_ip(request) == '203.0.113.1'
