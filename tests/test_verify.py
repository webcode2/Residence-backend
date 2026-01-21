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
