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
                "name": "Landlord 1", "phone": "+2348012345678",
                "house_number": "12", 
                "street_name": "Eborn"
            },
            {
                "email": f"landlord2-{uuid.uuid4().hex[:4]}@test.com", 
                "name": "Landlord 2", "phone": "+2348012345679",
                "house_number": "25", 
                "street_name": "SAL"
            }
        ]
        
        response = await client.post("/api/v1/landlords/import", json=import_data, headers=headers)
        
        assert response.status_code == 200
        assert len(response.json()) == 2
        assert response.json()[0]["status"] == "queued"
        
        # Queueing must return before account creation or email delivery.
        from sqlalchemy import select
        from app.models.onboarding import LandlordOnboardingJob
        jobs=(await db_session.execute(select(LandlordOnboardingJob))).scalars().all()
        assert len(jobs)==2 and jobs[0].status=='queued'
        assert mock_send.call_count==0

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

    # Generate a batch without relying on a missing external fixture file.
    import_data = [dict(name=f"Landlord {i}", email=f"landlord{i}@bulktest.com",
        phone="+2348012345678", house_number=str(i), street_name="Palm Avenue") for i in range(1,201)]

    # Mock notification service to avoid actual HTTP calls
    with patch("app.services.landlords.notification_service.send_registration_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        token = create_access_token({"sub": caretaker.email, "roles": [r.value for r in caretaker.roles], "app_id": app_id})
        headers = {"X-App-Id": app_id, "Authorization": f"Bearer {token}"}

        response = await client.post("/api/v1/landlords/import", json=import_data, headers=headers)
        
        assert response.status_code == 200
        assert len(response.json()) == 200
        assert all(res["status"] == "queued" for res in response.json())
        
        from sqlalchemy import select
        from app.models.onboarding import LandlordOnboardingJob
        jobs=(await db_session.execute(select(LandlordOnboardingJob))).scalars().all()
        assert len(jobs)==200 and all(job.status=='queued' for job in jobs)
        assert mock_send.call_count==0

@pytest.mark.asyncio
async def test_import_landlords_unauthorized(client, db_session):
    app_id = "RP-NONSTE"
    import_data = [{"email": "landlord@test.com", "name":"Test", "phone":"+2348012345678", "house_number":"12"}]
    headers = {"X-App-Id": app_id} # No auth token
    
    response = await client.post("/api/v1/landlords/import", json=import_data, headers=headers)
    assert response.status_code == 401
