import pytest
import uuid
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import RegistrationToken
from datetime import datetime, timedelta, UTC
from app.services.auth import create_access_token

@pytest.fixture
async def auth_headers(db_session):
    # Setup estate
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Test Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date())
    db_session.add(sub)
    
    # Setup caretaker
    caretaker = User(
        email=f"admin-{uuid.uuid4().hex[:4]}@test.com",
        full_name="Admin",
        hashed_password="hashed",
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(caretaker)
    await db_session.flush()
    
    token = create_access_token({"sub": caretaker.email, "app_id": app_id, "roles": [r.value for r in caretaker.roles]})
    return {"X-App-Id": app_id, "Authorization": f"Bearer {token}", "app_id": app_id, "caretaker_id": caretaker.id}

@pytest.mark.asyncio
async def test_register_tenant_success(client, db_session):
    reg_data = {
        "estateName": "New Estate",
        "adminName": "John Admin",
        "email": f"new-admin-{uuid.uuid4().hex[:4]}@test.com",
        "password": "password123",
        "terms": True
    }
    response = await client.post("/api/v1/users/register-tenant", json=reg_data)
    assert response.status_code == 200
    assert response.json()["app_id"].startswith("RP-")
    assert response.json()["estate_name"] == "New Estate"

@pytest.mark.asyncio
async def test_create_user_and_get(client, db_session, auth_headers):
    app_id = auth_headers["app_id"]
    headers = {k: v for k, v in auth_headers.items() if k != "app_id" and k != "caretaker_id"}
    
    # Create
    user_data = {
        "email": f"user-{uuid.uuid4().hex[:4]}@test.com",
        "password": "password123",
        "roles": [UserRole.LANDLORD.value],
        "app_id": app_id,
        "house_number": "Plot 7",
        "street_name": "Oak Close"
    }
    response = await client.post("/api/v1/users/", json=user_data, headers=headers)
    assert response.status_code == 200
    user_id = response.json()["id"]
    assert response.json()["house_number"] == "Plot 7"

    # Get
    response = await client.get(f"/api/v1/users/{user_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["email"] == user_data["email"]
    assert response.json()["street_name"] == "Oak Close"

@pytest.mark.asyncio
async def test_resident_registration_with_inheritance(client, db_session, auth_headers):
    app_id = auth_headers["app_id"]
    landlord_id = auth_headers["caretaker_id"]
    
    # Create registration token with property details
    # Example from user: SAL25 (SAL = street, 25 = house)
    reg_code = "REGABC123"
    token = RegistrationToken(
        code=reg_code, 
        landlord_id=landlord_id, 
        app_id=app_id,
        house_number="25",
        street_name="SAL"
    )
    db_session.add(token)
    await db_session.commit()

    # Register resident
    resident_data = {
        "email": f"res-{uuid.uuid4().hex[:4]}@test.com",
        "password": "respassword",
        "roles": [UserRole.RESIDENT.value],
        "app_id": app_id
    }
    response = await client.post(f"/api/v1/users/register?registration_code={reg_code}", json=resident_data, headers={"X-App-Id": app_id})
    
    assert response.status_code == 200
    # Verify inheritance
    assert response.json()["house_number"] == "25"
    assert response.json()["street_name"] == "SAL"
    assert response.json()["landlord_id"] == str(landlord_id)

@pytest.mark.asyncio
async def test_resident_registration_with_revoked_code(client, db_session, auth_headers):
    app_id = auth_headers["app_id"]
    landlord_id = auth_headers["caretaker_id"]
    
    reg_code = "REGREVOKED"
    token = RegistrationToken(code=reg_code, landlord_id=landlord_id, app_id=app_id, is_revoked=True)
    db_session.add(token)
    await db_session.commit()

    resident_data = {
        "email": "revoked@test.com",
        "password": "password123",
        "roles": [UserRole.RESIDENT.value],
        "app_id": app_id
    }
    response = await client.post(f"/api/v1/users/register?registration_code={reg_code}", json=resident_data, headers={"X-App-Id": app_id})
    
    assert response.status_code == 400
    assert "invalid registration code" in response.json()["detail"].lower()

@pytest.mark.asyncio
async def test_revoke_token_api(client, db_session, auth_headers):
    app_id = auth_headers["app_id"]
    headers = {k: v for k, v in auth_headers.items() if k != "app_id" and k != "caretaker_id"}
    landlord_id = auth_headers["caretaker_id"]
    
    reg_code = "REGTOREVOKE"
    token = RegistrationToken(code=reg_code, landlord_id=landlord_id, app_id=app_id)
    db_session.add(token)
    await db_session.commit()

    # Revoke via API
    response = await client.post(f"/api/v1/tokens/registration/{reg_code}/revoke", headers=headers)
    assert response.status_code == 200
    assert "revoked" in response.json()["message"].lower()

    # Verify in DB
    await db_session.refresh(token)
    assert token.is_revoked is True
