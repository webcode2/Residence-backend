import pytest
import uuid
from datetime import datetime, timedelta, date, UTC
from sqlalchemy import select

from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.access_log import AccessLog
from app.models.token import VisitorToken
from app.services.auth import get_saas_admin_password_hash, create_saas_admin_access_token
from app.services.maintenance import (
    purge_expired_access_logs,
    reset_billing_cycles_and_check_expirations,
    cleanup_expired_tokens,
    run_all_maintenance_jobs
)

@pytest.mark.asyncio
async def test_purge_expired_access_logs(db_session):
    # 1. Setup Estate with Starter tier (30 days retention)
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Retention Test Estate", is_active=True)
    db_session.add(estate)

    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=date.today() + timedelta(days=60),
        tier="starter",
        log_retention_days=30,
        price_monthly=15.00
    )
    db_session.add(sub)

    # 2. Add an old log (40 days ago, past the 30-day retention window)
    old_log = AccessLog(
        app_id=app_id,
        event_type="visitor_token_verification",
        identifier="ABCDEF",
        status="authorized",
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=40)
    )
    # Add a fresh log (5 days ago, well within the 30-day retention window)
    fresh_log = AccessLog(
        app_id=app_id,
        event_type="visitor_token_verification",
        identifier="GHIJKL",
        status="authorized",
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5)
    )
    db_session.add(old_log)
    db_session.add(fresh_log)
    await db_session.commit()

    # 3. Run purge routine
    purged_count = await purge_expired_access_logs(db_session)
    assert purged_count >= 1

    # 4. Verify only the fresh log remains
    remaining_logs = await db_session.execute(
        select(AccessLog).where(AccessLog.app_id == app_id)
    )
    logs = remaining_logs.scalars().all()
    assert len(logs) == 1
    assert logs[0].identifier == "GHIJKL"

@pytest.mark.asyncio
async def test_billing_cycles_and_subscription_expiration(db_session):
    # Setup Estate A: Billing cycle started 35 days ago (should reset quota)
    estate_a_id = uuid.uuid4()
    app_id_a = f"RP-{estate_a_id.hex[:5].upper()}"
    estate_a = Estate(id=estate_a_id, app_id=app_id_a, name="Cycle Reset Estate", is_active=True)
    db_session.add(estate_a)

    sub_a = Subscription(
        estate_id=estate_a_id,
        status="active",
        expiry_date=date.today() + timedelta(days=60),
        tier="standard",
        current_month_verifications=450,
        billing_cycle_start=date.today() - timedelta(days=35),
        price_monthly=30.00
    )
    db_session.add(sub_a)

    # Setup Estate B: Subscription expired yesterday
    estate_b_id = uuid.uuid4()
    app_id_b = f"RP-{estate_b_id.hex[:5].upper()}"
    estate_b = Estate(id=estate_b_id, app_id=app_id_b, name="Expired Estate", is_active=True)
    db_session.add(estate_b)

    sub_b = Subscription(
        estate_id=estate_b_id,
        status="active",
        expiry_date=date.today() - timedelta(days=1),
        tier="standard",
        price_monthly=30.00
    )
    db_session.add(sub_b)
    await db_session.commit()

    # Run maintenance routine
    stats = await reset_billing_cycles_and_check_expirations(db_session)
    assert stats["cycles_reset"] >= 1
    assert stats["subscriptions_expired"] >= 1

    # Verify changes
    await db_session.refresh(sub_a)
    assert sub_a.current_month_verifications == 0
    assert sub_a.billing_cycle_start == date.today()

    await db_session.refresh(sub_b)
    assert sub_b.status == "expired"

@pytest.mark.asyncio
async def test_cleanup_expired_tokens(db_session):
    resident_id = uuid.uuid4()
    app_id = "RP-TOKENS"

    # 1. Stale pending visitor token (past expires_at)
    stale_token = VisitorToken(
        code="STALE1",
        visitor_name="Late Visitor",
        resident_id=resident_id,
        app_id=app_id,
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
    )
    # 2. Checked-in visitor with expired bookout code
    stale_bookout = VisitorToken(
        code="ACTIVE",
        visitor_name="Departing Visitor",
        resident_id=resident_id,
        app_id=app_id,
        status="checked_in",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=10),
        bookout_code="OUT123",
        bookout_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=15)
    )
    db_session.add(stale_token)
    db_session.add(stale_bookout)
    await db_session.commit()

    token_stats = await cleanup_expired_tokens(db_session)
    assert token_stats["pending_tokens_expired"] >= 1
    assert token_stats["bookout_codes_cleared"] >= 1

    await db_session.refresh(stale_token)
    assert stale_token.status == "expired"

    await db_session.refresh(stale_bookout)
    assert stale_bookout.bookout_code is None

@pytest.mark.asyncio
async def test_admin_on_demand_maintenance_endpoint(client, db_session):
    # SaaS Admin auth
    admin_user = User(
        email="owner_maint@residencesaas.com",
        full_name="Platform Owner",
        hashed_password=get_saas_admin_password_hash("pass"),
        roles=[UserRole.SAAS_OWNER],
        app_id="GLOBAL"
    )
    db_session.add(admin_user)
    await db_session.commit()

    admin_token = create_saas_admin_access_token(data={"sub": admin_user.email, "roles": ["saas_owner"]})
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post("/api/v1/admin/maintenance/run", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "access_logs_purged" in data
    assert "billing_cycles_reset" in data
    assert "subscriptions_expired" in data
    assert "pending_tokens_expired" in data
    assert "duration_seconds" in data
