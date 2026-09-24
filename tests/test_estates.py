import pytest
import uuid
from app.models.user import User, UserRole

@pytest.mark.asyncio
async def test_register_estate_success(client, db_session):
    reg_data = {
        "estateName": f"Estate-{uuid.uuid4().hex[:4].upper()}",
        "adminName": "Peterson",
        "email": f"peterson-{uuid.uuid4().hex[:4]}@example.com",
        "password": "password123",
        "terms": True
    }
    response = await client.post("/api/v1/estates/register", json=reg_data)
    assert response.status_code == 200
    assert response.json()["estate_name"] == reg_data["estateName"]
    assert response.json()["app_id"].startswith("RP-")
    
    # Verify Admin User created
    from sqlalchemy import select
    from app.models.user import User
    result = await db_session.execute(select(User).where(User.email == reg_data["email"]))
    user = result.scalar_one_or_none()
    assert user is not None
    assert user.full_name == "Peterson"
    assert UserRole.CARETAKER in user.roles

@pytest.mark.asyncio
async def test_list_estates(client, db_session):
    response = await client.get("/api/v1/estates/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_estate_token_code_length_settings_and_az_generation(client, db_session):
    """
    Test that:
    1. Default token length is 6 uppercase A-Z characters.
    2. Admin can view settings via GET /api/v1/estates/settings.
    3. Admin can update token_code_length to 8 via PATCH /api/v1/estates/settings.
    4. New tokens use 8 uppercase A-Z characters.
    5. Invalid lengths (e.g. 5 or 9) are rejected with 422.
    """
    from datetime import datetime, timedelta, UTC
    from app.models.estate import Estate, Subscription
    from app.services.auth import create_access_token, get_password_hash

    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="AZ Token Estate", token_code_length=6)
    db_session.add(estate)

    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date(),
        tier="standard",
        monthly_verifications_limit=50000,
        current_month_verifications=0,
        rfid_enabled=True,
        realtime_alerts_enabled=True
    )
    db_session.add(sub)

    admin = User(
        email="admin@azestate.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(admin)
    await db_session.commit()

    admin_token = create_access_token(data={"sub": admin.email, "app_id": app_id, "roles": ["caretaker"]})
    headers = {"Authorization": f"Bearer {admin_token}", "X-App-Id": app_id}

    # 1. Fetch current settings -> default code length is 6
    settings_resp = await client.get("/api/v1/estates/settings", headers=headers)
    assert settings_resp.status_code == 200
    assert settings_resp.json()["token_code_length"] == 6

    # 2. Generate visitor token -> length must be 6, and only uppercase A-Z
    t1_resp = await client.post("/api/v1/tokens/visitor", json={"visitor_name": "Guest 6-char"}, headers=headers)
    assert t1_resp.status_code == 200
    code_6 = t1_resp.json()["code"]
    assert len(code_6) == 6
    assert code_6.isalpha() and code_6.isupper()

    # 3. Update estate setting to 8 characters
    patch_resp = await client.patch("/api/v1/estates/settings", json={"token_code_length": 8}, headers=headers)
    assert patch_resp.status_code == 200
    assert patch_resp.json()["token_code_length"] == 8

    # 4. Generate new visitor token -> length must now be 8, and only uppercase A-Z
    t2_resp = await client.post("/api/v1/tokens/visitor", json={"visitor_name": "Guest 8-char"}, headers=headers)
    assert t2_resp.status_code == 200
    code_8 = t2_resp.json()["code"]
    assert len(code_8) == 8
    assert code_8.isalpha() and code_8.isupper()

    # 5. Invalid length (e.g. 5) -> must be rejected (422)
    invalid_patch = await client.patch("/api/v1/estates/settings", json={"token_code_length": 5}, headers=headers)
    assert invalid_patch.status_code == 422

