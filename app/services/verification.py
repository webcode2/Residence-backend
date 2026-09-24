import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, date, UTC
from typing import Optional, Any
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.models.estate import Estate, Subscription
from app.services.notifications import notification_service
from app.services.access_logs import record_access_event
from app.services.token_state import persist_token_check_in, persist_token_check_out
from app.core.redis import (
    get_cached_subscription_state,
    set_cached_subscription_state,
    increment_monthly_quota_redis,
    reset_monthly_quota_redis,
    consume_cached_visitor_token,
    consume_cached_bookout_token,
    get_cached_rfid_user,
    set_cached_rfid_user,
    is_token_code_claimed,
    claim_token_code,
    get_token_live_status,
)

logger = logging.getLogger(__name__)


@dataclass
class GateSubscriptionState:
    """Lightweight subscription snapshot for the gate hot path (ORM-free)."""
    tier: str
    status: str
    realtime_alerts_enabled: bool
    rfid_enabled: bool
    monthly_verifications_limit: Optional[int]
    estate_id: Optional[uuid.UUID] = None
    from_cache: bool = False


def _state_from_cache(cached: dict) -> GateSubscriptionState:
    return GateSubscriptionState(
        tier=cached.get("tier", "starter"),
        status=cached.get("status", "unknown"),
        realtime_alerts_enabled=bool(cached.get("realtime_alerts_enabled")),
        rfid_enabled=bool(cached.get("rfid_enabled")),
        monthly_verifications_limit=cached.get("monthly_verifications_limit"),
        from_cache=True,
    )


def _state_from_sub(sub: Subscription) -> GateSubscriptionState:
    return GateSubscriptionState(
        tier=sub.tier,
        status=sub.status,
        realtime_alerts_enabled=bool(sub.realtime_alerts_enabled),
        rfid_enabled=bool(sub.rfid_enabled),
        monthly_verifications_limit=sub.monthly_verifications_limit,
        estate_id=sub.estate_id,
        from_cache=False,
    )


async def _enforce_quota(app_id: str, limit: Optional[int], tier: str, db: AsyncSession, is_rfid: bool) -> None:
    """Redis-first atomic quota. DB fallback only when Redis is unavailable."""
    allowed, count = await increment_monthly_quota_redis(app_id, limit)

    # count > 0 means Redis INCR succeeded (source of truth). Do not fall back to DB.
    if count > 0:
        if not allowed:
            await record_access_event(
                db,
                app_id=app_id,
                event_type="rfid_verification" if is_rfid else "visitor_token_verification",
                identifier="QUOTA_CHECK",
                status="denied",
                denial_reason=f"Monthly verification limit of {limit} exceeded on {tier} tier",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Monthly verification quota of {limit} exceeded on {tier} tier. Please upgrade your subscription.",
            )
        return

    # Redis unavailable: read-modify-write on Postgres (best-effort fallback)
    result = await db.execute(
        select(Subscription).join(Estate, Estate.id == Subscription.estate_id).where(Estate.app_id == app_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return
    if limit is not None and sub.current_month_verifications >= limit:
        await record_access_event(
            db,
            app_id=app_id,
            event_type="rfid_verification" if is_rfid else "visitor_token_verification",
            identifier="QUOTA_CHECK",
            status="denied",
            denial_reason=f"Monthly verification limit of {limit} exceeded on {tier} tier",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Monthly verification quota of {limit} exceeded on {tier} tier. Please upgrade your subscription.",
        )
    sub.current_month_verifications += 1
    await db.commit()


async def _check_and_update_subscription(
    db: AsyncSession, app_id: str, is_rfid: bool
) -> GateSubscriptionState:
    """
    Validates subscription active status, tier features, and monthly quota.
    Redis cache + INCR is the hot path; DB is only used on cache miss / Redis outage.
    """
    today = date.today()

    cached = await get_cached_subscription_state(app_id)
    if cached:
        if cached.get("status") != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Estate subscription is {cached.get('status')}. Access verification is disabled."
            )
        if cached.get("expiry_date") and cached["expiry_date"] < today.isoformat():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Estate subscription has expired. Access verification is disabled."
            )
        if is_rfid and not cached.get("rfid_enabled"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"RFID access is not supported on your current tier ({cached.get('tier')}). Please upgrade to Standard or higher."
            )

        state = _state_from_cache(cached)
        await _enforce_quota(app_id, state.monthly_verifications_limit, state.tier, db, is_rfid)
        return state

    # Cache miss: load from Postgres once, then cache for subsequent O(1) requests
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    if not estate or not estate.subscription:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Estate subscription not found. Access verification is disabled."
        )

    sub = estate.subscription

    if sub.status != "active":
        await record_access_event(
            db,
            app_id=app_id,
            event_type="rfid_verification" if is_rfid else "visitor_token_verification",
            identifier="SUB_STATUS_CHECK",
            status="denied",
            denial_reason=f"Subscription is {sub.status}",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Estate subscription is {sub.status}. Access verification is disabled."
        )

    if sub.expiry_date < today:
        await record_access_event(
            db,
            app_id=app_id,
            event_type="rfid_verification" if is_rfid else "visitor_token_verification",
            identifier="SUB_EXPIRY_CHECK",
            status="denied",
            denial_reason="Subscription has expired",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Estate subscription has expired. Access verification is disabled."
        )

    if (today - sub.billing_cycle_start).days >= 30:
        sub.current_month_verifications = 0
        sub.billing_cycle_start = today
        await db.commit()
        await reset_monthly_quota_redis(app_id)

    if is_rfid and not sub.rfid_enabled:
        await record_access_event(
            db,
            app_id=app_id,
            event_type="rfid_verification",
            identifier="RFID_SCAN",
            status="denied",
            denial_reason=f"RFID access is disabled on {sub.tier} tier",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"RFID access is not supported on your current tier ({sub.tier}). Please upgrade to Standard or higher."
        )

    await set_cached_subscription_state(app_id, {
        "tier": sub.tier,
        "status": sub.status,
        "expiry_date": sub.expiry_date.isoformat(),
        "monthly_verifications_limit": sub.monthly_verifications_limit,
        "rfid_enabled": sub.rfid_enabled,
        "log_retention_days": sub.log_retention_days,
        "realtime_alerts_enabled": sub.realtime_alerts_enabled,
    }, ttl_seconds=300)

    state = _state_from_sub(sub)
    await _enforce_quota(app_id, state.monthly_verifications_limit, state.tier, db, is_rfid)
    return state


async def verify_rfid_access(db: AsyncSession, rfid_tag: str, app_id: str) -> Optional[dict]:
    sub = await _check_and_update_subscription(db, app_id, is_rfid=True)

    # Fast path: Redis RFID profile
    cached_user = await get_cached_rfid_user(app_id, rfid_tag)
    if cached_user:
        if cached_user.get("is_revoked"):
            await record_access_event(
                db,
                app_id=app_id,
                event_type="rfid_verification",
                identifier=rfid_tag,
                status="denied",
                denial_reason="Invalid RFID tag or user revoked",
            )
            return None
        if cached_user.get("landlord_id") and cached_user.get("landlord_revoked"):
            await record_access_event(
                db,
                app_id=app_id,
                event_type="rfid_verification",
                identifier=rfid_tag,
                status="denied",
                denial_reason="Associated landlord revoked or inactive",
                user_id=cached_user.get("user_id"),
            )
            return None

        await record_access_event(
            db,
            app_id=app_id,
            event_type="rfid_verification",
            identifier=rfid_tag,
            status="authorized",
            user_id=cached_user.get("user_id"),
            details="RFID authorized via Redis cache",
        )

        if sub.realtime_alerts_enabled and cached_user.get("email"):
            asyncio.create_task(
                notification_service.send_realtime_access_alert(
                    cached_user["email"],
                    "RFID Gate Entry Authorized",
                    f"Your RFID tag was authorized for gate entry at {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}."
                )
            )

        return {
            "status": "authorized",
            "user_id": cached_user["user_id"],
            "roles": cached_user.get("roles", []),
        }

    # DB path (cache miss)
    result = await db.execute(
        select(User).where(
            User.app_id == app_id,
            User.rfid_tag == rfid_tag,
            User.is_revoked == False
        )
    )
    user = result.scalar_one_or_none()

    if not user:
        await record_access_event(
            db,
            app_id=app_id,
            event_type="rfid_verification",
            identifier=rfid_tag,
            status="denied",
            denial_reason="Invalid RFID tag or user revoked",
        )
        return None

    landlord_revoked = False
    if UserRole.RESIDENT in user.roles and user.landlord_id:
        land_result = await db.execute(select(User).where(User.id == user.landlord_id))
        landlord = land_result.scalar_one_or_none()
        if not landlord or landlord.is_revoked:
            landlord_revoked = True
            await record_access_event(
                db,
                app_id=app_id,
                event_type="rfid_verification",
                identifier=rfid_tag,
                status="denied",
                denial_reason="Associated landlord revoked or inactive",
                user_id=user.id,
            )
            return None

    roles = [r.value if hasattr(r, "value") else str(r) for r in user.roles]
    await set_cached_rfid_user(app_id, rfid_tag, {
        "user_id": str(user.id),
        "email": user.email,
        "roles": roles,
        "landlord_id": str(user.landlord_id) if user.landlord_id else None,
        "landlord_revoked": landlord_revoked,
        "is_revoked": False,
    }, ttl_seconds=120)

    await record_access_event(
        db,
        app_id=app_id,
        event_type="rfid_verification",
        identifier=rfid_tag,
        status="authorized",
        user_id=user.id,
    )

    if sub.realtime_alerts_enabled and user.email:
        asyncio.create_task(
            notification_service.send_realtime_access_alert(
                user.email,
                "RFID Gate Entry Authorized",
                f"Your RFID tag was authorized for gate entry at {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}."
            )
        )

    return {"status": "authorized", "user_id": str(user.id), "roles": roles}


async def verify_visitor_token(
    db: AsyncSession,
    code: str,
    app_id: str,
    direction: Optional[str] = None
) -> Optional[dict]:
    """
    Unified Single-Path Verification for Security Personnel.

    One-time use is enforced in Redis (cache GETDEL + claim keys).
    Postgres mark-used / check-out writes are enqueued for the worker.
    """
    if direction:
        direction = direction.lower().strip()
        if direction not in ["entry", "exit"]:
            direction = None

    code = code.upper().strip()
    sub = await _check_and_update_subscription(db, app_id, is_rfid=False)
    now = datetime.now(UTC).replace(tzinfo=None)

    if direction != "entry":
        cached_bookout = await consume_cached_bookout_token(app_id, code)
        if cached_bookout:
            token_id = uuid.UUID(cached_bookout["token_id"])
            resident_id = uuid.UUID(cached_bookout["resident_id"])
            visitor_name = cached_bookout["visitor_name"]

            await claim_token_code(app_id, code)
            ok = await persist_token_check_out(
                db,
                token_id=token_id,
                app_id=app_id,
                code=code,
                at=now,
                already_claimed=True,
            )
            if not ok:
                return None

            await record_access_event(
                db,
                app_id=app_id,
                event_type="bookout_verification",
                identifier=code,
                status="authorized",
                resident_id=resident_id,
                visitor_name=visitor_name,
                details="Exit book-out authorized via Redis fresh cache (< 20m); Postgres write queued",
            )

            if sub.realtime_alerts_enabled and resident_id:
                res_result = await db.execute(select(User).where(User.id == resident_id))
                resident = res_result.scalar_one_or_none()
                if resident and resident.email:
                    asyncio.create_task(
                        notification_service.send_realtime_access_alert(
                            resident.email,
                            f"Visitor Checked Out: {visitor_name}",
                            f"Visitor '{visitor_name}' has checked out and departed through the exit gate at {now.strftime('%Y-%m-%d %H:%M:%S UTC')}."
                        )
                    )

            return {
                "status": "authorized",
                "action": "book_out",
                "visitor_name": visitor_name,
                "resident_id": str(resident_id),
                "checked_out_at": now.isoformat(),
                "message": "Exit authorized (Book-Out successful)"
            }

        bookout_res = await db.execute(
            select(VisitorToken).where(
                VisitorToken.bookout_code == code,
                VisitorToken.app_id == app_id,
                VisitorToken.status == "checked_in",
                VisitorToken.bookout_expires_at > now
            )
        )
        bookout_token = bookout_res.scalar_one_or_none()

        if not bookout_token and direction == "exit":
            # Exit via original entry code — honor live Redis check-in (Postgres may lag)
            alt_res = await db.execute(
                select(VisitorToken).where(
                    VisitorToken.code == code,
                    VisitorToken.app_id == app_id,
                )
            )
            candidate = alt_res.scalar_one_or_none()
            if candidate:
                live = await get_token_live_status(str(candidate.id))
                effective = (live or {}).get("status") or candidate.status
                if effective == "checked_in":
                    bookout_token = candidate
        if bookout_token:
            if await is_token_code_claimed(app_id, code):
                return None
            ok = await persist_token_check_out(
                db,
                token_id=bookout_token.id,
                app_id=app_id,
                code=code,
                at=now,
            )
            if not ok:
                return None

            await record_access_event(
                db,
                app_id=app_id,
                event_type="bookout_verification",
                identifier=code,
                status="authorized",
                resident_id=bookout_token.resident_id,
                visitor_name=bookout_token.visitor_name,
                details="Exit book-out authorized via PostgreSQL lookup; Postgres write queued",
            )

            if sub.realtime_alerts_enabled and bookout_token.resident_id:
                res_result = await db.execute(select(User).where(User.id == bookout_token.resident_id))
                resident = res_result.scalar_one_or_none()
                if resident and resident.email:
                    asyncio.create_task(
                        notification_service.send_realtime_access_alert(
                            resident.email,
                            f"Visitor Checked Out: {bookout_token.visitor_name}",
                            f"Visitor '{bookout_token.visitor_name}' has checked out and departed through the exit gate at {now.strftime('%Y-%m-%d %H:%M:%S UTC')}."
                        )
                    )

            return {
                "status": "authorized",
                "action": "book_out",
                "visitor_name": bookout_token.visitor_name,
                "resident_id": str(bookout_token.resident_id),
                "checked_out_at": now.isoformat(),
                "message": "Exit authorized (Book-Out successful)"
            }

    if direction != "exit":
        # Reject codes already claimed (queued write not yet flushed)
        if await is_token_code_claimed(app_id, code):
            await record_access_event(
                db,
                app_id=app_id,
                event_type="visitor_token_verification",
                identifier=code,
                status="denied",
                denial_reason="Invalid, expired, or already used visitor token/book-out code",
            )
            return None

        cached_entry = await consume_cached_visitor_token(app_id, code)
        if cached_entry:
            token_id = uuid.UUID(cached_entry["id"])
            resident_id = uuid.UUID(cached_entry["resident_id"])
            visitor_name = cached_entry["visitor_name"]

            await claim_token_code(app_id, code)
            ok = await persist_token_check_in(
                db,
                token_id=token_id,
                app_id=app_id,
                code=code,
                at=now,
                already_claimed=True,
            )
            if not ok:
                return None

            await record_access_event(
                db,
                app_id=app_id,
                event_type="visitor_token_verification",
                identifier=code,
                status="authorized",
                resident_id=resident_id,
                visitor_name=visitor_name,
                details="Fast-path entry verification via Redis fresh cache (< 20m); Postgres write queued",
            )

            if sub.realtime_alerts_enabled and resident_id:
                res_result = await db.execute(select(User).where(User.id == resident_id))
                resident = res_result.scalar_one_or_none()
                if resident and resident.email:
                    asyncio.create_task(
                        notification_service.send_realtime_access_alert(
                            resident.email,
                            f"Visitor Arrived: {visitor_name}",
                            f"Visitor '{visitor_name}' just arrived at the gate and checked in using visitor code {code}."
                        )
                    )

            return {
                "status": "authorized",
                "action": "check_in",
                "visitor_name": visitor_name,
                "resident_id": str(resident_id),
                "checked_in_at": now.isoformat(),
                "message": "Entry authorized (Check-In successful)"
            }

        entry_res = await db.execute(
            select(VisitorToken).where(
                VisitorToken.code == code,
                VisitorToken.app_id == app_id,
                VisitorToken.is_used == False,
                VisitorToken.expires_at > now
            )
        )
        entry_token = entry_res.scalar_one_or_none()
        if entry_token:
            ok = await persist_token_check_in(
                db,
                token_id=entry_token.id,
                app_id=app_id,
                code=code,
                at=now,
            )
            if not ok:
                return None

            await record_access_event(
                db,
                app_id=app_id,
                event_type="visitor_token_verification",
                identifier=code,
                status="authorized",
                resident_id=entry_token.resident_id,
                visitor_name=entry_token.visitor_name,
                details="Standard entry verification via PostgreSQL lookup; Postgres write queued",
            )

            if sub.realtime_alerts_enabled and entry_token.resident_id:
                res_result = await db.execute(select(User).where(User.id == entry_token.resident_id))
                resident = res_result.scalar_one_or_none()
                if resident and resident.email:
                    asyncio.create_task(
                        notification_service.send_realtime_access_alert(
                            resident.email,
                            f"Visitor Arrived: {entry_token.visitor_name}",
                            f"Visitor '{entry_token.visitor_name}' just arrived at the gate and checked in using visitor code {code}."
                        )
                    )

            return {
                "status": "authorized",
                "action": "check_in",
                "visitor_name": entry_token.visitor_name,
                "resident_id": str(entry_token.resident_id),
                "checked_in_at": now.isoformat(),
                "message": "Entry authorized (Check-In successful)"
            }

    await record_access_event(
        db,
        app_id=app_id,
        event_type="visitor_token_verification",
        identifier=code,
        status="denied",
        denial_reason="Invalid, expired, or already used visitor token/book-out code",
    )
    return None


async def verify_bookout_token(db: AsyncSession, code: str, app_id: str) -> Optional[dict]:
    """Alias for backwards compatibility and dedicated exit barrier hardware."""
    return await verify_visitor_token(db, code, app_id, direction="exit")
