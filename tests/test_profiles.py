import uuid
from datetime import timedelta
from jose import jwt
import pytest
from sqlalchemy import select
from app.models.base import utc_now
from app.models.estate import Estate
from app.models.user import User, UserRole
from app.models.token import RegistrationToken
from app.services.auth import create_access_token, get_password_hash

@pytest.fixture
async def profile_members(db_session):
    app_id='PROFILE-'+uuid.uuid4().hex[:8]
    db_session.add(Estate(name='Profile test', app_id=app_id,is_active=True))
    host=User(email=uuid.uuid4().hex+'@example.com',full_name='Original Host',phone='08000000000',roles=[UserRole.LANDLORD],app_id=app_id,house_number='12',street_name='Old street',hashed_password=get_password_hash('profile-test-password'))
    db_session.add(host);await db_session.flush()
    member=User(email=uuid.uuid4().hex+'@example.com',full_name='Member',roles=[UserRole.RESIDENT],landlord_id=host.id,app_id=app_id,house_number='12',street_name='Old street',hashed_password='unused')
    db_session.add(member)
    invite=RegistrationToken(code=uuid.uuid4().hex[:9].upper(),landlord_id=host.id,app_id=app_id,house_number='12',street_name='Old street',expires_at=utc_now()+timedelta(days=1))
    db_session.add(invite);await db_session.commit()
    token=create_access_token({'sub':host.email,'app_id':app_id,'roles':['landlord']},expires_delta=timedelta(days=20))
    return host,member,invite,token,{'X-App-Id':app_id,'Authorization':'Bearer '+token}

@pytest.mark.asyncio
async def test_self_profile_updates_persist_and_propagate_property_without_extending_expiry(client,db_session,profile_members):
    host,member,invite,token,headers=profile_members
    response=await client.patch('/api/v1/users/me/profile',headers=headers,json={'full_name':'Updated Host','phone':'+2348012345678','house_number':'20B','street_name':'New street'})
    assert response.status_code==200,response.text
    data=response.json();assert data['user']['phone']=='+2348012345678' and data['user']['house_number']=='20B'
    assert data['user']['roles']==['landlord']
    await db_session.refresh(member);await db_session.refresh(invite)
    assert member.house_number=='20B' and invite.house_number=='20B'
    assert member.landlord_id==host.id
    fresh=data['token']['access_token']
    assert abs(jwt.get_unverified_claims(fresh)['exp']-jwt.get_unverified_claims(token)['exp'])<=1
    assert (await client.get('/api/v1/users/me/profile',headers=headers)).status_code==401
    saved=await client.get('/api/v1/users/me/profile',headers={**headers,'Authorization':'Bearer '+fresh})
    assert saved.status_code==200 and saved.json()['full_name']=='Updated Host'

@pytest.mark.asyncio
async def test_email_change_requires_password_and_rotates_signin_identity(client,profile_members):
    host,_,_,_,headers=profile_members
    email=uuid.uuid4().hex+'@example.com'
    failed=await client.patch('/api/v1/users/me/profile',headers=headers,json={'email':email})
    assert failed.status_code==400
    response=await client.patch('/api/v1/users/me/profile',headers=headers,json={'email':email,'current_password':'profile-test-password'})
    assert response.status_code==200,response.text
    assert response.json()['user']['email']==email
    assert jwt.get_unverified_claims(response.json()['token']['access_token'])['sub']==email
    login=await client.post('/api/v1/auth/login',headers={'X-App-Id':host.app_id},json={'email':email,'password':'profile-test-password'})
    assert login.status_code==200,login.text

@pytest.mark.asyncio
@pytest.mark.parametrize('forged',[{'roles':['saas_owner']},{'app_id':'OTHER'},{'landlord_id':str(uuid.uuid4())},{'rfid_tag':'FORGED'}])
async def test_self_edit_cannot_change_security_assignments(client,profile_members,forged):
    *_,headers=profile_members
    response=await client.patch('/api/v1/users/me/profile',headers=headers,json=forged)
    assert response.status_code==422,response.text

@pytest.mark.asyncio
async def test_duplicate_email_does_not_save_any_profile_changes(client,db_session,profile_members):
    host,member,_,_,headers=profile_members
    response=await client.patch('/api/v1/users/me/profile',headers=headers,json={'email':member.email,'full_name':'Not saved','current_password':'profile-test-password'})
    assert response.status_code==409
    await db_session.refresh(host)
    assert host.full_name=='Original Host'


def test_digit_dominant_code_composition():
    from app.services.tokens import generate_visitor_code,generate_registration_code
    for length in (4,5,6,8,10):
        for _ in range(40):
            code=generate_visitor_code(length)
            assert sum(c.isalpha() for c in code)==1
            assert sum(c.isdigit() for c in code)==length-1
    invitation=generate_registration_code()
    assert len(invitation)==9 and sum(c.isalpha() for c in invitation)==1


def test_session_identity_cannot_follow_recycled_email():
    from app.services.auth import token_matches_user
    from datetime import UTC, datetime
    original_id = uuid.uuid4()
    issued = datetime.now(UTC)
    replacement = User(id=uuid.uuid4(), created_at=issued + timedelta(seconds=1))
    assert not token_matches_user({'uid': str(original_id)}, replacement)
    assert not token_matches_user({'iat': issued.timestamp()}, replacement)
    assert not token_matches_user({'sub': 'reused@example.com'}, replacement)
    assert token_matches_user({'uid': str(replacement.id)}, replacement)
    assert token_matches_user({'iat': (issued + timedelta(seconds=2)).timestamp()}, replacement)


@pytest.mark.asyncio
async def test_code_collisions_never_change_estate_length(monkeypatch):
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from app.services import tokens
    lengths = []
    def generate(length):
        lengths.append(length)
        return 'A' + '1' * (length - 1)
    monkeypatch.setattr(tokens, 'generate_visitor_code', generate)
    monkeypatch.setattr(tokens, 'is_visitor_code_active', AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as error:
        await tokens.generate_unique_visitor_code(None, 'test', length=6, max_retries=3)
    assert error.value.status_code == 503
    assert lengths == [6, 6, 6]
