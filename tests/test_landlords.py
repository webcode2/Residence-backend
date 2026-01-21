import pytest
import uuid
import csv
import io
from unittest.mock import AsyncMock, patch
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from datetime import datetime, timedelta, UTC
from app.services.auth import create_access_token

@pytest.mark.asyncio
async def test_import_landlords_success(client, db_session):
    # Setup estate
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Import Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date())
    db_session.add(sub)
    
    # Setup caretaker (authorized user)
    caretaker = User(
        email="caretaker@import.com",
        full_name="Caretaker",
        hashed_password="hashed",
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(caretaker)
    await db_session.commit()

    # Mock notification service
    with patch("app.services.landlords.notification_service.send_registration_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        # Mocking auth for the test
        token = create_access_token({"sub": caretaker.email, "roles": [r.value for r in caretaker.roles], "app_id": app_id})
        headers = {"X-App-Id": app_id, "Authorization": f"Bearer {token}"}

        import_data = [
            {
                "email": f"landlord1-{uuid.uuid4().hex[:4]}@test.com", 
                "name": "Landlord 1",
                "house_number": "12", 
                "street_name": "Eborn"
            },
            {
                "email": f"landlord2-{uuid.uuid4().hex[:4]}@test.com", 
                "name": "Landlord 2",
                "house_number": "25", 
                "street_name": "SAL"
            }
        ]
        
        response = await client.post("/api/v1/landlords/import", json=import_data, headers=headers)
        
        assert response.status_code == 200
        assert len(response.json()) == 2
        assert response.json()[0]["status"] == "success"
        
        # Verify users created in DB with property details
        from sqlalchemy import select
        result = await db_session.execute(select(User).where(User.email == import_data[0]["email"]))
        user1 = result.scalar_one_or_none()
        assert user1 is not None
        assert user1.house_number == "12"
        assert user1.street_name == "Eborn"
        assert UserRole.LANDLORD in user1.roles
        
        # Verify email was "sent"
        assert mock_send.call_count == 2

@pytest.mark.asyncio
async def test_import_landlords_bulk_200(client, db_session):
    """Test importing 200 landlords from the generated CSV file."""
    # Setup estate
    estate_id = uuid.uuid4()
    app_id = f"RP-BULK"
    estate = Estate(id=estate_id, app_id=app_id, name="Bulk Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date())
    db_session.add(sub)
    
    # Setup caretaker
    caretaker = User(
        email="admin@bulk.com",
        full_name="Bulk Admin",
        hashed_password="hashed",
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(caretaker)
    await db_session.commit()

    # Read from the CSV file created earlier
    import_data = []
    with open("landlords_200.csv", mode='r') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            import_data.append(row)

    # Mock notification service to avoid actual HTTP calls
    with patch("app.services.landlords.notification_service.send_registration_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        token = create_access_token({"sub": caretaker.email, "roles": [r.value for r in caretaker.roles], "app_id": app_id})
        headers = {"X-App-Id": app_id, "Authorization": f"Bearer {token}"}

        response = await client.post("/api/v1/landlords/import", json=import_data, headers=headers)
        
        assert response.status_code == 200
        assert len(response.json()) == 200
        assert all(res["status"] == "success" for res in response.json())
        
        # Verify a random user from the list
        from sqlalchemy import select
        result = await db_session.execute(select(User).where(User.email == "landlord100@bulktest.com"))
        user = result.scalar_one_or_none()
        assert user is not None
        assert UserRole.LANDLORD in user.roles
        assert user.app_id == app_id

@pytest.mark.asyncio
async def test_import_landlords_unauthorized(client, db_session):
    app_id = "RP-NONSTE"
    import_data = [{"email": "landlord@test.com"}]
    headers = {"X-App-Id": app_id} # No auth token
    
    response = await client.post("/api/v1/landlords/import", json=import_data, headers=headers)
    assert response.status_code == 401
