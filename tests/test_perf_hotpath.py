import pytest
import uuid
from datetime import datetime, timedelta, UTC
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.access_log import AccessLog
from app.services.auth import get_password_hash
from app.services.access_logs import record_access_event, drain_access_log_queue
from app.core.redis import (
    get_redis_client,
    ACCESS_LOG_QUEUE_KEY,
    get_monthly_quota_redis,
    increment_monthly_quota_redis,
    set_cached_rfid_user,
    get_cached_rfid_user,
    invalidate_cached_rfid_user,
)


@pytest.mark.asyncio
async def test_access_log_enqueue_and_drain(db_session):
    """Access logs should land in Redis then flush to Postgres via drain."""
    client = get_redis_client()
    if not client:
        pytest.skip("Redis not available")

    await client.delete(ACCESS_LOG_QUEUE_KEY)
    app_id = f"RP-{uuid.uuid4().hex[:5].upper()}"

    await record_access_event(
        app_id=app_id,
        event_type="rfid_verification",
        identifier="TAG-ASYNC",
        status="authorized",
        details="queued",
    )

    queued = await client.llen(ACCESS_LOG_QUEUE_KEY)
    assert queued >= 1

    written = await drain_access_log_queue(db_session, batch_size=50)
    assert written >= 1

    from sqlalchemy import select
    result = await db_session.execute(
        select(AccessLog).where(AccessLog.app_id == app_id, AccessLog.identifier == "TAG-ASYNC")
    )
    log = result.scalar_one_or_none()
    assert log is not None
    assert log.status == "authorized"


@pytest.mark.asyncio
async def test_redis_quota_is_atomic_source_of_truth():
    client = get_redis_client()
    if not client:
        pytest.skip("Redis not available")

    app_id = f"RP-{uuid.uuid4().hex[:5].upper()}"
    allowed, count = await increment_monthly_quota_redis(app_id, limit=5)
    assert allowed is True
    assert count == 1
    assert await get_monthly_quota_redis(app_id) == 1

    for _ in range(4):
        await increment_monthly_quota_redis(app_id, limit=5)
    allowed, count = await increment_monthly_quota_redis(app_id, limit=5)
    assert allowed is False
    assert count == 6


@pytest.mark.asyncio
async def test_rfid_cache_roundtrip():
    client = get_redis_client()
    if not client:
        pytest.skip("Redis not available")

    app_id = f"RP-{uuid.uuid4().hex[:5].upper()}"
    tag = "CACHE-TAG-1"
    await invalidate_cached_rfid_user(app_id, tag)
    await set_cached_rfid_user(app_id, tag, {
        "user_id": str(uuid.uuid4()),
        "email": "a@b.com",
        "roles": ["resident"],
        "landlord_id": None,
        "landlord_revoked": False,
        "is_revoked": False,
    }, ttl_seconds=60)
    cached = await get_cached_rfid_user(app_id, tag)
    assert cached is not None
    assert cached["email"] == "a@b.com"
    await invalidate_cached_rfid_user(app_id, tag)
    assert await get_cached_rfid_user(app_id, tag) is None


@pytest.mark.asyncio
async def test_verify_rfid_still_works_end_to_end(client, db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Perf Estate")
    db_session.add(estate)
    sub = Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
        tier="standard",
        rfid_enabled=True,
        monthly_verifications_limit=50000,
    )
    db_session.add(sub)
    resident = User(
        email=f"res-{app_id}@test.com",
        hashed_password=get_password_hash("pass"),
        roles=[UserRole.RESIDENT],
        rfid_tag=f"RFID-{app_id}",
        app_id=app_id,
    )
    db_session.add(resident)
    await db_session.commit()

    headers = {"X-App-Id": app_id}
    response = await client.get(f"/api/v1/verify/rfid/RFID-{app_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "authorized"

    # Second hit should succeed via RFID cache when Redis is up
    response2 = await client.get(f"/api/v1/verify/rfid/RFID-{app_id}", headers=headers)
    assert response2.status_code == 200


@pytest.mark.asyncio
async def test_token_state_queued_then_drained(db_session):
    """Successful check-in enqueues Postgres write; drain applies it."""
    from app.core.redis import get_redis_client, TOKEN_STATE_QUEUE_KEY, token_state_queue_length
    from app.services.token_state import persist_token_check_in, drain_token_state_queue
    from app.models.token import VisitorToken

    client = get_redis_client()
    if not client:
        pytest.skip("Redis not available")

    await client.delete(TOKEN_STATE_QUEUE_KEY)

    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:5].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Queue Estate")
    db_session.add(estate)
    db_session.add(Subscription(
        estate_id=estate_id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
    ))
    resident = User(
        email=f"q-{app_id}@t.com",
        hashed_password="x",
        roles=[UserRole.RESIDENT],
        app_id=app_id,
    )
    db_session.add(resident)
    await db_session.flush()
    token = VisitorToken(
        code="QQTESTA",
        visitor_name="Queued Guest",
        resident_id=resident.id,
        app_id=app_id,
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2),
        status="pending",
        is_used=False,
    )
    db_session.add(token)
    await db_session.commit()

    ok = await persist_token_check_in(
        db_session, token_id=token.id, app_id=app_id, code="QQTESTA"
    )
    assert ok is True
    assert await token_state_queue_length() >= 1

    written = await drain_token_state_queue(db_session)
    assert written >= 1
    await db_session.refresh(token)
    assert token.status == "checked_in"
    assert token.is_used is True
