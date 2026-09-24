import pytest
import uuid
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.services.auth import get_password_hash
from datetime import datetime, timedelta, UTC

@pytest.mark.asyncio
async def test_login_success(client, db_session):
    # Setup estate and user
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Auth Test Estate")
    db_session.add(estate)
    
    sub = Subscription(
        estate_id=estate_id, 
        status="active", 
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date()
    )
    db_session.add(sub)
    
    user = User(
        email="test@auth.com",
        full_name="Test User",
        hashed_password=get_password_hash("secret123"),
        roles=[UserRole.LANDLORD],
        app_id=app_id
    )
    db_session.add(user)
    await db_session.commit()

    # Test login with JSON
    headers = {"X-App-Id": app_id}
    login_data = {
        "email": "test@auth.com",
        "password": "secret123"
    }
    response = await client.post("/api/v1/auth/login", json=login_data, headers=headers)
    
    assert response.status_code == 200
    assert response.json()["token"]["access_token"] is not None
    assert response.json()["token"]["token_type"] == "bearer"
    assert response.json()["user"]["email"] == "test@auth.com"
    assert response.json()["user"]["full_name"] == "Test User"
    assert response.json()["user"]["roles"] == ["landlord"]
    assert response.json()["user"]["app_id"] == app_id
    assert response.json()["user"]["id"] is not None
    assert response.json()["user"]["is_revoked"] is False
    assert response.json()["user"]["landlord_id"] is None
    assert response.json()["user"]["created_at"] is not None

@pytest.mark.asyncio
async def test_login_invalid_credentials(client, db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Auth Fail Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date())
    db_session.add(sub)
    await db_session.commit()

    headers = {"X-App-Id": app_id}
    login_data = {"email": "wrong@test.com", "password": "wrongpassword"}
    response = await client.post("/api/v1/auth/login", json=login_data, headers=headers)
    
    assert response.status_code == 401
    assert "incorrect email or password" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_redis_cached_jwt_and_logout(client, db_session):
    # 1. Setup Estate and Caretaker
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Redis Auth Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date())
    db_session.add(sub)

    user = User(
        email="caretaker@redisauth.com",
        full_name="Caretaker Boss",
        hashed_password=get_password_hash("pass123"),
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(user)
    await db_session.commit()

    # 2. Login
    headers = {"X-App-Id": app_id}
    login_resp = await client.post("/api/v1/auth/login", json={"email": "caretaker@redisauth.com", "password": "pass123"}, headers=headers)
    assert login_resp.status_code == 200
    token = login_resp.json()["token"]["access_token"]
    user_id = login_resp.json()["user"]["id"]

    # 3. Authenticated request using JWT - loads from Redis cache
    auth_headers = {"Authorization": f"Bearer {token}", "X-App-Id": app_id}
    resp = await client.get(f"/api/v1/users/{user_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "caretaker@redisauth.com"

    # 4. Logout (blacklists token in Redis and invalidates user cache)
    logout_resp = await client.post("/api/v1/auth/logout", headers=auth_headers)
    assert logout_resp.status_code == 200
    assert "logged out" in logout_resp.json()["message"].lower()

    # 5. Subsequent request with the same token MUST be rejected (401)
    reused_resp = await client.get(f"/api/v1/users/{user_id}", headers=auth_headers)
    assert reused_resp.status_code == 401
    assert "revoked or logged out" in reused_resp.json()["detail"].lower()

