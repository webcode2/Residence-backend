import pytest
import uuid
from datetime import datetime, timedelta, date, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.services.auth import (
    create_access_token, 
    get_password_hash, 
    get_saas_admin_password_hash,
    verify_saas_admin_password
)
from app.core.tiers import SubscriptionTier

@pytest.mark.asyncio
async def test_saas_admin_full_workflow(client, db_session):
    # 1. Setup SaaS Owner with dedicated peppered hash and an Estate with Subscription
    saas_owner = User(
        email="owner@residencesaas.com",
        full_name="Platform Owner",
        hashed_password=get_saas_admin_password_hash("superadmin123"),
        roles=[UserRole.SAAS_OWNER],
        app_id="GLOBAL"
    )
    db_session.add(saas_owner)

    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Sunset Ridge", is_active=True)
    db_session.add(estate)

    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date(),
        tier="starter",
        monthly_verifications_limit=20000,
        current_month_verifications=150,
        rfid_enabled=False,
        realtime_alerts_enabled=False,
        price_monthly=15.00
    )
    db_session.add(sub)

    resident = User(
        email="res@sunset.com",
        full_name="Alice Resident",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    # 2. SaaS Admin Login via dedicated /admin/auth/login (No X-App-Id required)
    login_resp = await client.post("/api/v1/admin/auth/login", json={
        "email": "owner@residencesaas.com",
        "password": "superadmin123"
    })
    assert login_resp.status_code == 200
    admin_token = login_resp.json()["token"]["access_token"]
    assert "saas_owner" in login_resp.json()["user"]["roles"]

    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 3. Regular user rejected from /admin/auth/login
    fail_login = await client.post("/api/v1/admin/auth/login", json={
        "email": "res@sunset.com",
        "password": "pass"
    })
    assert fail_login.status_code in [401, 403]

    # 4. SaaS Admin Profile check (/admin/auth/me)
    me_resp = await client.get("/api/v1/admin/auth/me", headers=admin_headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "owner@residencesaas.com"
    assert "saas_owner" in me_resp.json()["roles"]

    # 5. SaaS Overview / Analytics KPIs
    overview_resp = await client.get("/api/v1/admin/overview", headers=admin_headers)
    assert overview_resp.status_code == 200
    overview_data = overview_resp.json()
    assert overview_data["total_estates"] >= 1
    assert overview_data["total_users"] >= 2
    assert overview_data["estimated_mrr"] >= 15.00
    assert overview_data["total_verifications_this_month"] >= 150

    # 6. List Estates via Admin endpoint
    estates_resp = await client.get("/api/v1/admin/estates", headers=admin_headers)
    assert estates_resp.status_code == 200
    estates_list = estates_resp.json()
    assert len(estates_list) >= 1
    found_estate = next((e for e in estates_list if e["app_id"] == app_id), None)
    assert found_estate is not None
    assert found_estate["name"] == "Sunset Ridge"
    assert found_estate["user_count"] >= 1

    # 7. Suspend Estate
    suspend_resp = await client.patch(f"/api/v1/admin/estates/{estate_id}/status", json={"is_active": False}, headers=admin_headers)
    assert suspend_resp.status_code == 200
    assert suspend_resp.json()["is_active"] is False

    # 8. Override Subscription (Upgrade directly to Enterprise with unlimited verifications)
    override_resp = await client.patch(
        f"/api/v1/admin/subscriptions/{estate_id}",
        json={
            "tier": "enterprise",
            "monthly_verifications_limit": None,
            "rfid_enabled": True
        },
        headers=admin_headers
    )
    assert override_resp.status_code == 200
    assert override_resp.json()["tier"] == "enterprise"
    assert override_resp.json()["monthly_verifications_limit"] is None
    assert override_resp.json()["rfid_enabled"] is True

    # 9. Cross-Tenant User Search
    users_resp = await client.get(f"/api/v1/admin/users?query=Alice", headers=admin_headers)
    assert users_resp.status_code == 200
    assert len(users_resp.json()) == 1
    assert users_resp.json()[0]["email"] == "res@sunset.com"

    # 10. Revoke User Account
    revoke_resp = await client.patch(
        f"/api/v1/admin/users/{resident.id}/revoke",
        json={"is_revoked": True},
        headers=admin_headers
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["is_revoked"] is True

    # 11. Cryptographic Isolation Test: Regular token signed with SECRET_KEY CANNOT access admin endpoints
    # Even if regular token claims "saas_owner", the signature fails because SAAS_ADMIN_SECRET_KEY is different
    forged_token = create_access_token(data={"sub": "owner@residencesaas.com", "app_id": "GLOBAL", "roles": ["saas_owner"]})
    forged_headers = {"Authorization": f"Bearer {forged_token}"}
    forged_resp = await client.get("/api/v1/admin/overview", headers=forged_headers)
    assert forged_resp.status_code == 401 # Rejected because SECRET_KEY != SAAS_ADMIN_SECRET_KEY!

    # 12. SaaS Admin Change Password flow
    change_pwd_resp = await client.post(
        "/api/v1/admin/auth/change-password",
        json={"current_password": "superadmin123", "new_password": "newsuperadminpassword456"},
        headers=admin_headers
    )
    assert change_pwd_resp.status_code == 200

    # Verify new password works and old password fails
    re_login_fail = await client.post("/api/v1/admin/auth/login", json={
        "email": "owner@residencesaas.com",
        "password": "superadmin123"
    })
    assert re_login_fail.status_code == 401

    re_login_success = await client.post("/api/v1/admin/auth/login", json={
        "email": "owner@residencesaas.com",
        "password": "newsuperadminpassword456"
    })
    assert re_login_success.status_code == 200
    new_admin_token = re_login_success.json()["token"]["access_token"]

    # 13. SaaS Admin Logout flow
    new_admin_headers = {"Authorization": f"Bearer {new_admin_token}"}
    logout_resp = await client.post("/api/v1/admin/auth/logout", headers=new_admin_headers)
    assert logout_resp.status_code == 200

    # Token immediately blacklisted in Redis
    blacklisted_resp = await client.get("/api/v1/admin/overview", headers=new_admin_headers)
    assert blacklisted_resp.status_code == 401
