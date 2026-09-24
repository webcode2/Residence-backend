import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.core.tiers import SubscriptionTier, get_tier_config
from app.services.auth import get_password_hash, create_access_token

@pytest.mark.asyncio
async def test_list_tiers(client):
    response = await client.get("/api/v1/subscriptions/tiers")
    assert response.status_code == 200
    tiers = response.json()
    assert len(tiers) == 4
    
    tier_map = {t["tier"]: t for t in tiers}
    # Starter
    assert tier_map["starter"]["price_monthly"] == 15.0
    assert tier_map["starter"]["monthly_verifications_limit"] == 20000
    assert tier_map["starter"]["rfid_enabled"] is False
    assert tier_map["starter"]["log_retention_days"] == 30
    assert tier_map["starter"]["realtime_alerts_enabled"] is False

    # Standard
    assert tier_map["standard"]["price_monthly"] == 30.0
    assert tier_map["standard"]["monthly_verifications_limit"] == 50000
    assert tier_map["standard"]["rfid_enabled"] is True
    assert tier_map["standard"]["log_retention_days"] == 90
    assert tier_map["standard"]["realtime_alerts_enabled"] is True

    # Premium
    assert tier_map["premium"]["price_monthly"] == 70.0
    assert tier_map["premium"]["monthly_verifications_limit"] == 300000
    assert tier_map["premium"]["rfid_enabled"] is True
    assert tier_map["premium"]["log_retention_days"] == 180
    assert tier_map["premium"]["realtime_alerts_enabled"] is True

    # Enterprise
    assert tier_map["enterprise"]["price_monthly"] == 100.0
    assert tier_map["enterprise"]["monthly_verifications_limit"] is None
    assert tier_map["enterprise"]["rfid_enabled"] is True
    assert tier_map["enterprise"]["log_retention_days"] == 365
    assert tier_map["enterprise"]["realtime_alerts_enabled"] is True

@pytest.mark.asyncio
async def test_rfid_disabled_on_starter_tier(client, db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Starter Estate")
    db_session.add(estate)

    tier_cfg = get_tier_config(SubscriptionTier.STARTER)
    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
        tier=tier_cfg["tier"],
        monthly_verifications_limit=tier_cfg["monthly_verifications_limit"],
        rfid_enabled=tier_cfg["rfid_enabled"],
        log_retention_days=tier_cfg["log_retention_days"],
        realtime_alerts_enabled=tier_cfg["realtime_alerts_enabled"],
        price_monthly=tier_cfg["price_monthly"]
    )
    db_session.add(sub)

    resident = User(
        email="res_starter@test.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        rfid_tag="RFID_STARTER_1",
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    # Attempt to scan RFID tag on Starter plan ($15/mo)
    headers = {"X-App-Id": app_id}
    response = await client.get("/api/v1/verify/rfid/RFID_STARTER_1", headers=headers)
    assert response.status_code == 403
    assert "RFID access is not supported on your current tier" in response.json()["detail"]

@pytest.mark.asyncio
async def test_verification_quota_limit_enforced(client, db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Quota Estate")
    db_session.add(estate)

    # Set subscription with limit of 2, already at 2
    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
        tier="starter",
        monthly_verifications_limit=2,
        current_month_verifications=2,
        rfid_enabled=False
    )
    db_session.add(sub)
    await db_session.commit()

    headers = {"X-App-Id": app_id}
    response = await client.get("/api/v1/verify/token/9999", headers=headers)
    assert response.status_code == 403
    assert "Monthly verification quota" in response.json()["detail"]

@pytest.mark.asyncio
async def test_token_generation_blocked_when_quota_exhausted(client, db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Token Quota Estate")
    db_session.add(estate)

    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
        tier="starter",
        monthly_verifications_limit=10,
        current_month_verifications=10
    )
    db_session.add(sub)

    resident = User(
        email="res_tok@test.com",
        hashed_password="hash",
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    token = create_access_token(data={"sub": resident.email, "app_id": app_id, "roles": ["resident"]})
    headers = {"Authorization": f"Bearer {token}", "X-App-Id": app_id}

    response = await client.post(
        "/api/v1/tokens/visitor",
        json={"visitor_name": "Alice Guest"},
        headers=headers
    )
    assert response.status_code == 403
    assert "quota" in response.json()["detail"].lower()
