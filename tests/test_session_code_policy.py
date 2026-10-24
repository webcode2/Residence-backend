from datetime import datetime, UTC
from unittest.mock import AsyncMock
import uuid
import pytest
from jose import jwt
from app.models.user import User, UserRole
from app.models.estate import Estate
from app.services.auth import get_password_hash
from app.services.tokens import generate_visitor_code


def test_passcodes_always_mix_letters_and_digits():
    for length in (6, 8):
        codes = [generate_visitor_code(length) for _ in range(200)]
        assert all(len(code) == length and code.isalnum() and code.isupper() and any(c.isdigit() for c in code) and any(c.isalpha() for c in code) for code in codes)


@pytest.mark.asyncio
async def test_regular_login_last_60_days_and_logout_revokes_remaining_lifetime(client, db_session, monkeypatch):
    app_id='SESSION-'+uuid.uuid4().hex[:8]
    estate=Estate(name='Session test',app_id=app_id,is_active=True)
    user=User(email=uuid.uuid4().hex+'@example.com',app_id=app_id,hashed_password=get_password_hash('session-test-password'),roles=[UserRole.RESIDENT])
    db_session.add_all([estate,user]);await db_session.commit()
    response=await client.post('/api/v1/auth/login',headers={'X-App-Id':app_id},json={'email':user.email,'password':'session-test-password'})
    assert response.status_code==200,response.text
    token=response.json()['token']['access_token']
    seconds=jwt.get_unverified_claims(token)['exp']-datetime.now(UTC).timestamp()
    assert 60*86400-60 < seconds <= 60*86400
    mock=AsyncMock()
    monkeypatch.setattr('app.core.redis.blacklist_token',mock)
    out=await client.post('/api/v1/auth/logout',headers={'X-App-Id':app_id,'Authorization':'Bearer '+token})
    assert out.status_code==200,out.text
    assert mock.call_args.kwargs['ttl_seconds'] > 59*86400


@pytest.mark.asyncio
async def test_first_login_restriction_remains_30_minutes(client,db_session):
    app_id='RESTRICTED-'+uuid.uuid4().hex[:8]
    db_session.add(Estate(name='Restricted session test',app_id=app_id,is_active=True))
    user=User(email=uuid.uuid4().hex+'@example.com',app_id=app_id,hashed_password=get_password_hash('temporary-test-password'),roles=[UserRole.LANDLORD],must_change_password=True)
    db_session.add(user);await db_session.commit()
    response=await client.post('/api/v1/auth/login',headers={'X-App-Id':app_id},json={'email':user.email,'password':'temporary-test-password'})
    assert response.status_code==200,response.text
    claims=jwt.get_unverified_claims(response.json()['token']['access_token'])
    assert 1740 < claims['exp']-datetime.now(UTC).timestamp() <= 1800
    assert claims['purpose']=='password_change'
