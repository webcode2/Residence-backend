import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole

@pytest.mark.asyncio
async def test_create_and_list_billings(client, db_session):
    # Setup estate and resident
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Billing Test Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date())
    db_session.add(sub)
    
    resident = User(email="res@billing.com", hashed_password="hashed", roles=[UserRole.RESIDENT], app_id=app_id)
    db_session.add(resident)
    await db_session.commit()

    headers = {"X-App-Id": app_id}
    
    # Create billing
    billing_data = {
        "user_id": str(resident.id),
        "amount": 150.0,
        "title": "Monthly Security Fee",
        "description": "Payment for Jan 2026",
        "app_id": app_id
    }
    response = await client.post("/api/v1/billings/", json=billing_data, headers=headers)
    assert response.status_code == 200
    
    # List all
    response = await client.get("/api/v1/billings/", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1

    # List by user
    response = await client.get(f"/api/v1/billings/user/{resident.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["amount"] == 150.0
