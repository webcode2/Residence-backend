import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.access_log import AccessLog
from app.services.auth import create_access_token

@pytest.mark.asyncio
async def test_access_log_retention_window(client, db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Log Estate")
    db_session.add(estate)

    # Starter tier has 30 days retention
    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date(),
        tier="starter",
        log_retention_days=30
    )
    db_session.add(sub)

    # Admin user
    admin = User(
        email="admin_log@test.com",
        hashed_password="hash",
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(admin)
    await db_session.flush()

    # Create one recent log (1 day ago) and one expired log (45 days ago)
    now = datetime.now(UTC).replace(tzinfo=None)
    recent_log = AccessLog(
        app_id=app_id,
        event_type="visitor_token_verification",
        identifier="1234",
        status="authorized",
        created_at=now - timedelta(days=1),
        visitor_name="Recent Visitor"
    )
    expired_log = AccessLog(
        app_id=app_id,
        event_type="visitor_token_verification",
        identifier="5678",
        status="authorized",
        created_at=now - timedelta(days=45),
        visitor_name="Old Visitor"
    )
    db_session.add(recent_log)
    db_session.add(expired_log)
    await db_session.commit()

    token = create_access_token(data={"sub": admin.email, "app_id": app_id, "roles": ["caretaker"]})
    headers = {"Authorization": f"Bearer {token}", "X-App-Id": app_id}

    response = await client.get("/api/v1/access-logs/", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["tier"] == "starter"
    assert data["log_retention_days"] == 30
    assert data["total_returned"] == 1
    assert data["logs"][0]["visitor_name"] == "Recent Visitor"
