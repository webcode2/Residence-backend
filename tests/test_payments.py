import pytest
import uuid
import hmac
import hashlib
import json
from datetime import datetime, timedelta, date, UTC
from unittest.mock import patch, AsyncMock

from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.services.auth import create_access_token, get_password_hash
from app.core.tiers import SubscriptionTier
from app.core.config import settings

@pytest.mark.asyncio
async def test_bachs_checkout_and_webhook_flow(client, db_session):
    # 1. Setup Estate and Caretaker (Estate Admin)
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"

    estate = Estate(id=estate_id, app_id=app_id, name="Royal Palms Estate", is_active=True)
    db_session.add(estate)

    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=10)).date(),
        tier="starter",
        monthly_verifications_limit=20000,
        current_month_verifications=500,
        rfid_enabled=False,
        realtime_alerts_enabled=False,
        price_monthly=15.00
    )
    db_session.add(sub)

    caretaker = User(
        email="admin@royalpalms.com",
        full_name="Estate Caretaker",
        hashed_password=get_password_hash("securepass123"),
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db_session.add(caretaker)

    resident = User(
        email="resident@royalpalms.com",
        full_name="Bob Resident",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    caretaker_token = create_access_token(data={"sub": caretaker.email, "app_id": app_id, "roles": ["caretaker"]})
    resident_token = create_access_token(data={"sub": resident.email, "app_id": app_id, "roles": ["resident"]})

    admin_headers = {"Authorization": f"Bearer {caretaker_token}", "X-App-Id": app_id}
    resident_headers = {"Authorization": f"Bearer {resident_token}", "X-App-Id": app_id}

    # 2. Non-admin (resident) forbidden from creating checkout session
    forbidden_resp = await client.post(
        "/api/v1/subscriptions/checkout",
        json={"tier": "premium"},
        headers=resident_headers
    )
    assert forbidden_resp.status_code == 403

    # 3. Estate Admin creates checkout session (simulated sandbox mode)
    checkout_resp = await client.post(
        "/api/v1/subscriptions/checkout",
        json={
            "tier": "premium",
            "success_url": "https://royalpalms.com/billing/success",
            "cancel_url": "https://royalpalms.com/billing/cancel"
        },
        headers=admin_headers
    )
    assert checkout_resp.status_code == 200
    checkout_data = checkout_resp.json()
    assert checkout_data["tier"] == "premium"
    assert checkout_data["amount"] == 70.00
    assert "checkout.bachs.io" in checkout_data["checkout_url"]
    session_id = checkout_data["session_id"]

    # 4. Verify checkout session endpoint
    verify_resp = await client.get(
        f"/api/v1/subscriptions/checkout/{session_id}/verify",
        headers=admin_headers
    )
    assert verify_resp.status_code == 200
    verify_data = verify_resp.json()
    assert verify_data["is_active"] is True
    assert verify_data["status"] == "paid"

    # Check updated subscription details
    sub_resp = await client.get("/api/v1/subscriptions/current", headers=admin_headers)
    assert sub_resp.status_code == 200
    current_sub = sub_resp.json()
    assert current_sub["tier"] == "starter" or current_sub["tier"] == "premium"

    # 5. Live Bachs Checkout Mock Test
    mock_post_resp = AsyncMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {
        "id": "cs_live_998877",
        "checkout_url": "https://checkout.bachs.io/cs_live_998877",
        "status": "open"
    }
    mock_post_resp.raise_for_status = AsyncMock()

    with patch("app.core.config.settings.BACHS_SECRET_KEY", "sk_test_bachs_secret"):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_http_post:
            mock_http_post.return_value = mock_post_resp
            live_resp = await client.post(
                "/api/v1/payments/checkout",
                json={"tier": "enterprise"},
                headers=admin_headers
            )
            assert live_resp.status_code == 200
            assert live_resp.json()["session_id"] == "cs_live_998877"
            assert live_resp.json()["amount"] == 100.00

    # 6. Bachs Webhook: Signature verification and fulfillment to Enterprise
    webhook_secret = "test_webhook_signing_secret"
    with patch("app.core.config.settings.BACHS_WEBHOOK_SECRET", webhook_secret):
        webhook_payload = {
            "event": "collection.succeeded",
            "data": {
                "id": "cs_live_998877",
                "status": "paid",
                "metadata": {
                    "app_id": app_id,
                    "tier": "enterprise",
                    "estate_id": str(estate_id)
                }
            }
        }
        body_bytes = json.dumps(webhook_payload).encode("utf-8")
        valid_sig = hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

        # 6a. Invalid signature rejected
        invalid_resp = await client.post(
            "/api/v1/subscriptions/webhook/bachs",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Bachs-Signature": "invalid_sig"}
        )
        assert invalid_resp.status_code == 400

        # 6b. Valid signature succeeds and upgrades subscription
        valid_resp = await client.post(
            "/api/v1/subscriptions/webhook/bachs",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Bachs-Signature": valid_sig}
        )
        assert valid_resp.status_code == 200
        assert valid_resp.json()["status"] == "success"
        assert valid_resp.json()["tier"] == "enterprise"

    # 7. Verify subscription upgraded to Enterprise in database
    sub_after_webhook = await client.get("/api/v1/subscriptions/current", headers=admin_headers)
    assert sub_after_webhook.status_code == 200
    sub_data = sub_after_webhook.json()
    assert sub_data["tier"] == "enterprise"
    assert sub_data["monthly_verifications_limit"] is None # Unlimited
    assert sub_data["rfid_enabled"] is True
    assert sub_data["realtime_alerts_enabled"] is True
    assert sub_data["current_month_verifications"] == 0 # Reset quota
    assert sub_data["price_monthly"] == 100.00
