import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.services.auth import get_password_hash

@pytest.mark.asyncio
async def test_verify_rfid_success(client, db_session, guard_headers):
    # Setup
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Test Estate")
    db_session.add(estate)
    
    sub = Subscription(
        estate_id=estate_id, 
        status="active", 
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date()
    )
    db_session.add(sub)
    
    resident = User(
        email="res@test.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        rfid_tag="TAG123",
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.commit()

    # Test
    headers = await guard_headers(app_id)
    response = await client.get("/api/v1/verify/rfid/TAG123", headers=headers)
    
    assert response.status_code == 200
    assert response.json()["status"] == "authorized"

@pytest.mark.asyncio
async def test_verify_token_expired(client, db_session, guard_headers):
    # Setup
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Test Estate")
    db_session.add(estate)
    
    sub = Subscription(
        estate_id=estate_id, 
        status="active", 
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)).date()
    )
    db_session.add(sub)
    
    resident = User(
        email="res2@test.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        app_id=app_id
    )
    db_session.add(resident)
    await db_session.flush()
    
    token = VisitorToken(
        code="9999",
        visitor_name="Guest",
        resident_id=resident.id,
        app_id=app_id,
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1) # Expired
    )
    db_session.add(token)
    await db_session.commit()

    # Test
    headers = await guard_headers(app_id)
    response = await client.get("/api/v1/verify/token/9999", headers=headers)
    
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()
    assert response.headers["X-Residence-Verification-Result"] == "token_expired"


@pytest.mark.asyncio
async def test_code_uniqueness_enforcement(client, db_session, guard_headers):
    """Ensure active entry codes and active exit codes never collide."""
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Unique Code Estate")
    db_session.add(estate)
    sub = Subscription(estate_id=estate_id, status="active", expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date())
    db_session.add(sub)

    from app.services.tokens import is_visitor_code_active, generate_unique_visitor_code

    resident = User(email=f"unique-{uuid.uuid4().hex}@test.com", hashed_password="hash", roles=[UserRole.RESIDENT], app_id=app_id)
    db_session.add(resident)
    await db_session.flush()
    token = VisitorToken(
        resident_id=resident.id,
        code="ABCXYZ",
        visitor_name="Guest 1",
        app_id=app_id,
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)
    )
    db_session.add(token)
    await db_session.commit()

    # "ABCXYZ" is active as an entry code
    assert await is_visitor_code_active(db_session, app_id, "ABCXYZ") is True
    # Case-insensitivity check
    assert await is_visitor_code_active(db_session, app_id, "abcxyz") is True
    # "ZZZZZZ" is not active
    assert await is_visitor_code_active(db_session, app_id, "ZZZZZZ") is False

    # Generated unique code should never pick "ABCXYZ"
    unique_code = await generate_unique_visitor_code(db_session, app_id)
    assert unique_code != "ABCXYZ"
    assert len(unique_code) == 6
    assert unique_code.isalnum() and any(c.isdigit() for c in unique_code) and any(c.isalpha() for c in unique_code) and unique_code.isupper()


@pytest.mark.asyncio
async def test_unrelated_misses_do_not_block_entry_or_exit(client, db_session, guard_headers):
    from app.core.redis import cache_bookout_token, get_verification_code_cache_state

    now = datetime.now(UTC).replace(tzinfo=None)
    estate = Estate(app_id=f"VERIFY-{uuid.uuid4().hex[:8]}", name="Verification Estate")
    db_session.add(estate)
    await db_session.flush()
    db_session.add(Subscription(
        estate_id=estate.id, status="active",
        expiry_date=(now + timedelta(days=1)).date(),
    ))
    resident = User(
        email="guest-host@example.com", hashed_password="unused",
        roles=[UserRole.RESIDENT], app_id=estate.app_id,
    )
    db_session.add(resident)
    await db_session.flush()

    def token(code, **options):
        fields = {
            "resident_id": resident.id, "visitor_name": "Guest",
            "app_id": estate.app_id, "expires_at": now + timedelta(hours=1),
        }
        return VisitorToken(code=code, **(fields | options))

    entry = token("0A1234")
    exit_pass = token(
        "0B1234", status="checked_in", is_used=True, bookout_code="0C1234",
        bookout_expires_at=now + timedelta(hours=1),
    )
    expired_exit = token(
        "0D1234", status="checked_in", is_used=True, bookout_code="0E1234",
        bookout_expires_at=now - timedelta(minutes=1),
    )
    db_session.add_all([entry, exit_pass, expired_exit])
    await db_session.commit()
    headers = await guard_headers(estate.app_id)
    other_guard = await guard_headers(estate.app_id)

    assert await get_verification_code_cache_state(estate.app_id, entry.code) == "absent"
    assert await cache_bookout_token(estate.app_id, exit_pass.bookout_code, {
        "token_id": str(exit_pass.id), "resident_id": str(resident.id),
        "visitor_name": exit_pass.visitor_name,
    })
    assert await get_verification_code_cache_state(estate.app_id, exit_pass.bookout_code) == "present"

    result_header = "X-Residence-Verification-Result"
    for code in ("0001", "0002", "0003", "0004", "0005", "0006"):
        missing = await client.get(f"/api/v1/verify/token/{code}", headers=headers)
        assert missing.status_code == 401
        assert missing.headers[result_header] == "token_not_found"
        assert missing.json()["detail"] == "Token not found"
        assert missing.json()["status_code"] == 401
        assert missing.json()["request_id"]

    wrong_lane = await client.get(
        "/api/v1/verify/token/0C1234?direction=entry", headers=headers,
    )
    assert wrong_lane.status_code == 401
    assert wrong_lane.headers[result_header] == "token_denied"
    assert await get_verification_code_cache_state(estate.app_id, "0C1234") == "present"
    assert await get_verification_code_cache_state("OTHER-ESTATE", "0C1234") == "absent"

    expired = await client.get("/api/v1/verify/book-out/0E1234", headers=headers)
    assert expired.status_code == 401
    assert expired.headers[result_header] == "token_expired"
    used = await client.get("/api/v1/verify/token/0D1234", headers=headers)
    assert used.status_code == 401
    assert used.headers[result_header] == "token_used"

    checked_in = await client.get("/api/v1/verify/token/0a1234", headers=other_guard)
    assert checked_in.status_code == 200
    assert checked_in.json()["action"] == "check_in"
    repeated = await client.get("/api/v1/verify/token/0A1234", headers=other_guard)
    assert repeated.status_code == 401
    assert repeated.headers[result_header] == "token_claimed"

    checked_out = await client.get("/api/v1/verify/book-out/0C1234", headers=other_guard)
    assert checked_out.status_code == 200
    assert checked_out.json()["action"] == "book_out"


@pytest.mark.asyncio
@pytest.mark.parametrize(('action', 'title', 'verb'), [
    ('check_in', 'Visitor arrival', 'checked in'),
    ('book_out', 'Visitor departure', 'checked out'),
])
async def test_visitor_access_names_guest_in_inbox_and_push(db_session, monkeypatch, action, title, verb):
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.api.v1.endpoints.verify import _notify_visitor_access
    from app.models.sos import AppNotification
    from app.services import notification_queue

    app_id = 'RP-VISITOR'
    resident = User(email='resident@visitor.example', app_id=app_id,
                    roles=[UserRole.RESIDENT], hashed_password='unused', fcm_token='test-device')
    db_session.add(resident)
    await db_session.commit()
    dispatch = AsyncMock()
    monkeypatch.setattr(notification_queue, 'dispatch', dispatch)

    await _notify_visitor_access(db_session, app_id, {
        'action': action, 'resident_id': resident.id, 'visitor_name': 'Ada Okafor',
        'message': 'Generic verification result',
    })

    row = await db_session.scalar(select(AppNotification).where(AppNotification.user_id == resident.id))
    assert row.title == title
    assert row.body == f'Ada Okafor {verb} at the gate.'
    dispatch.assert_awaited_once_with(
        'push', token='test-device', title=title, body=row.body,
        data={**row.data, 'notification_count': 1},
    )
    assert row.data['notification_event_key'] == row.event_key
