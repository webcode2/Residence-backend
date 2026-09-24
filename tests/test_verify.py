import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.services.auth import get_password_hash

@pytest.mark.asyncio
async def test_verify_rfid_success(client, db_session):
    # Setup
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Test Estate")
    db_session.add(estate)
    
    sub = Subscription(
        estate_id=estate_id, 
        status="active", 
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date()
    )
    db_session.add(sub)
    
    resident = User(
        email="res@test.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        rfid_tag="TAG123",
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    # Test
    headers = {"X-App-Id": app_id}
    response = await client.get("/api/v1/verify/rfid/TAG123", headers=headers)
    
    assert response.status_code == 200
    assert response.json()["status"] == "authorized"

@pytest.mark.asyncio
async def test_verify_token_expired(client, db_session):
    # Setup
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Test Estate")
    db_session.add(estate)
    
    sub = Subscription(
        estate_id=estate_id, 
        status="active", 
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date()
    )
    db_session.add(sub)
    
    resident = User(
        email="res2@test.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.flush()
    
    token = VisitorToken(
        code="9999",
        visitor_name="Guest",
        resident_id=resident.id,
        app_id=app_id,
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1) # Expired
    )
    db_session.add(token)
    await db_session.commit()

    # Test
    headers = {"X-App-Id": app_id}
    response = await client.get("/api/v1/verify/token/9999", headers=headers)
    
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_code_uniqueness_enforcement(client, db_session):
    """Ensure active entry codes and active exit codes never collide."""
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Unique Code Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date())
    db_session.add(sub)

    from app.services.tokens import is_visitor_code_active, generate_unique_visitor_code

    token = VisitorToken(
        code="ABCXYZ",
        visitor_name="Guest 1",
        app_id=app_id,
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)
    )
    db_session.add(token)
    await db_session.commit()

    # "ABCXYZ" is active as an entry code
    assert await is_visitor_code_active(db_session, app_id, "ABCXYZ") is True
    # Case-insensitivity check
    assert await is_visitor_code_active(db_session, app_id, "abcxyz") is True
    # "ZZZZZZ" is not active
    assert await is_visitor_code_active(db_session, app_id, "ZZZZZZ") is False

    # Generated unique code should never pick "ABCXYZ"
    unique_code = await generate_unique_visitor_code(db_session, app_id)
    assert unique_code != "ABCXYZ"
    assert len(unique_code) == 6
    assert unique_code.isalpha() and unique_code.isupper()


@pytest.mark.asyncio
async def test_verification_rate_limiting_after_5_failures(client, db_session):
    """Ensure 5 consecutive failed verification attempts temporarily blocks the gate terminal (429)."""
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Rate Limit Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date())
    db_session.add(sub)
    await db_session.commit()

    headers = {"X-App-Id": app_id}

    # Attempts 1 to 4: Should return 401 Unauthorized with remaining attempts
    for i in range(1, 5):
        resp = await client.get(f"/api/v1/verify/token/000{i}", headers=headers)
        assert resp.status_code == 401
        assert "attempts remaining" in resp.json()["detail"].lower()

    # Attempt 5: Reaches threshold and triggers temporary block (429)
    resp_5 = await client.get("/api/v1/verify/token/0005", headers=headers)
    assert resp_5.status_code == 429
    assert "temporarily blocked" in resp_5.json()["detail"].lower()

    # Attempt 6 (while blocked): Immediate 429 Too Many Requests
    resp_6 = await client.get("/api/v1/verify/token/0006", headers=headers)
    assert resp_6.status_code == 429
    assert "blocked" in resp_6.json()["detail"].lower()

