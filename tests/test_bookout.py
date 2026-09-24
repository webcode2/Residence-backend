import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.services.auth import create_access_token

@pytest.mark.asyncio
async def test_full_visitor_lifecycle_and_bookout(client, db_session):
    # 1. Setup Estate and Resident
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Bookout Estate")
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

    resident = User(
        email="resident_bookout@test.com",
        hashed_password="hash",
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    token = create_access_token(data={"sub": resident.email, "app_id": app_id, "roles": ["resident"]})
    headers = {"Authorization": f"Bearer {token}", "X-App-Id": app_id}

    # 2. Resident creates visitor token (Status: pending)
    resp = await client.post("/api/v1/tokens/visitor", json={"visitor_name": "Delivery Person"}, headers=headers)
    assert resp.status_code == 200
    token_data = resp.json()
    token_id = token_data["id"]
    entry_code = token_data["code"]
    assert token_data["status"] == "pending"

    # Attempt to book out before checking in -> should be rejected (400)
    resp_premature = await client.post(f"/api/v1/tokens/visitor/{token_id}/book-out", headers=headers)
    assert resp_premature.status_code == 400
    assert "not checked in" in resp_premature.json()["detail"].lower()

    # 3. Visitor arrives at ENTRY gate (GET /api/v1/verify/token/{code})
    gate_headers = {"X-App-Id": app_id}
    resp_entry = await client.get(f"/api/v1/verify/token/{entry_code}", headers=gate_headers)
    assert resp_entry.status_code == 200
    assert resp_entry.json()["status"] == "authorized"
    assert resp_entry.json()["action"] == "check_in"

    # Verify status changed to 'checked_in' in database
    token_db = await db_session.get(VisitorToken, uuid.UUID(token_id))
    await db_session.refresh(token_db)
    assert token_db.status == "checked_in"
    assert token_db.checked_in_at is not None

    # 4. Resident generates fresh book-out code for this token
    resp_bookout_gen = await client.post(f"/api/v1/tokens/visitor/{token_id}/book-out", headers=headers)
    assert resp_bookout_gen.status_code == 200
    bookout_data = resp_bookout_gen.json()
    bookout_code = bookout_data["bookout_code"]
    assert len(bookout_code) == 6
    assert bookout_data["token_id"] == token_id

    # 5. Visitor arrives at EXIT gate:
    # Test using the UNIFIED SINGLE ROUTE /verify/token/{code} without guard switching screens!
    resp_exit_unified = await client.get(f"/api/v1/verify/token/{bookout_code}", headers=gate_headers)
    assert resp_exit_unified.status_code == 200
    assert resp_exit_unified.json()["status"] == "authorized"
    assert resp_exit_unified.json()["action"] == "book_out"

    # Verify status updated to 'checked_out'
    await db_session.refresh(token_db)
    assert token_db.status == "checked_out"
    assert token_db.checked_out_at is not None

    # 6. Attempt to reuse book-out code -> should be rejected (401)
    resp_reuse = await client.get(f"/api/v1/verify/token/{bookout_code}", headers=gate_headers)
    assert resp_reuse.status_code == 401


@pytest.mark.asyncio
async def test_security_operations_workflow(client, db_session):
    """
    Test that security personnel (UserRole.SECURITY) can execute the full operation:
    1. View active visitor roster (GET /tokens/visitor/active)
    2. Generate book-out token for any visitor (POST /tokens/visitor/{token_id}/book-out)
    3. Perform direct one-tap checkout at exit gate (POST /tokens/visitor/{token_id}/checkout)
    4. Exit gate verification using original code with direction=exit
    """
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Security Gate Estate")
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

    # Create security user and resident
    security_user = User(
        email="guard1@estate.com",
        hashed_password="hash",
        roles=[UserRole.SECURITY],
        app_id=app_id
    )
    resident = User(
        email="resident1@estate.com",
        hashed_password="hash",
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add_all([security_user, resident])
    await db_session.commit()

    sec_token = create_access_token(data={"sub": security_user.email, "app_id": app_id, "roles": ["security"]})
    sec_headers = {"Authorization": f"Bearer {sec_token}", "X-App-Id": app_id}
    gate_headers = {"X-App-Id": app_id}

    # Create a visitor checked in
    v_token = VisitorToken(
        id=uuid.uuid4(),
        app_id=app_id,
        resident_id=resident.id,
        visitor_name="Guest Alpha",
        code="7788",
        status="checked_in",
        is_used=True,
        checked_in_at=datetime.now(UTC).replace(tzinfo=None),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=4)
    )
    db_session.add(v_token)
    await db_session.commit()

    # 1. Security views the live active on-premises visitor roster
    roster_resp = await client.get("/api/v1/tokens/visitor/active", headers=sec_headers)
    assert roster_resp.status_code == 200
    active_list = roster_resp.json()
    assert len(active_list) == 1
    assert active_list[0]["visitor_name"] == "Guest Alpha"

    # 2. Security generates bookout code from their gate terminal for this visitor
    bookout_resp = await client.post(f"/api/v1/tokens/visitor/{v_token.id}/book-out", headers=sec_headers)
    assert bookout_resp.status_code == 200
    assert "bookout_code" in bookout_resp.json()

    # 3. Security performs direct one-tap checkout at the exit gate
    checkout_resp = await client.post(f"/api/v1/tokens/visitor/{v_token.id}/checkout", headers=sec_headers)
    assert checkout_resp.status_code == 200
    assert checkout_resp.json()["action"] == "book_out"

    # Verify visitor is no longer in active roster
    roster_after = await client.get("/api/v1/tokens/visitor/active", headers=sec_headers)
    assert roster_after.status_code == 200
    assert len(roster_after.json()) == 0

