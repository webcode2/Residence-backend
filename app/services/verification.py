import asyncio
from datetime import datetime, date, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status
from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.models.estate import Estate, Subscription
from app.models.access_log import AccessLog
from app.services.notifications import notification_service
from app.core.redis import (
    get_cached_subscription_state,
    set_cached_subscription_state,
    increment_monthly_quota_redis,
    consume_cached_visitor_token,
    consume_cached_bookout_token
)
from typing import Optional
import uuid

async def _check_and_update_subscription(db: AsyncSession, app_id: str, is_rfid: bool) -> tuple[Estate | None, Subscription | None]:
    """
    Validates subscription active status, tier features, and monthly quota.
    Leverages Redis for sub-millisecond caching and atomic quota increments.
    """
    today = date.today()

    # 1. Fast path: Redis cache check
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
        # Atomic Redis quota check
        allowed, count = await increment_monthly_quota_redis(app_id, cached.get("monthly_verifications_limit"))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Monthly verification quota of {cached.get('monthly_verifications_limit')} exceeded on {cached.get('tier')} tier. Please upgrade your subscription."
            )

    # 2. Database path (cache miss or fallback)
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    if not estate or not estate.subscription:
        return estate, None

    sub = estate.subscription

    # Check subscription status and expiry
    if sub.status != "active":
        db.add(AccessLog(
            app_id=app_id,
            event_type="rfid_verification" if is_rfid else "visitor_token_verification",
            identifier="SUB_STATUS_CHECK",
            status="denied",
            denial_reason=f"Subscription is {sub.status}"
        ))
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Estate subscription is {sub.status}. Access verification is disabled."
        )

    if sub.expiry_date < today:
        db.add(AccessLog(
            app_id=app_id,
            event_type="rfid_verification" if is_rfid else "visitor_token_verification",
            identifier="SUB_EXPIRY_CHECK",
            status="denied",
            denial_reason="Subscription has expired"
        ))
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Estate subscription has expired. Access verification is disabled."
        )

    # Reset quota if 30 days have elapsed since billing cycle start
    if (today - sub.billing_cycle_start).days >= 30:
        sub.current_month_verifications = 0
        sub.billing_cycle_start = today

    # Check RFID entitlement by tier
    if is_rfid and not sub.rfid_enabled:
        db.add(AccessLog(
            app_id=app_id,
            event_type="rfid_verification",
            identifier="RFID_SCAN",
            status="denied",
            denial_reason=f"RFID access is disabled on {sub.tier} tier"
        ))
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"RFID access is not supported on your current tier ({sub.tier}). Please upgrade to Standard or higher."
        )

    # Check monthly quota
    if sub.monthly_verifications_limit is not None:
        if sub.current_month_verifications >= sub.monthly_verifications_limit:
            db.add(AccessLog(
                app_id=app_id,
                event_type="rfid_verification" if is_rfid else "visitor_token_verification",
                identifier="QUOTA_CHECK",
                status="denied",
                denial_reason=f"Monthly verification limit of {sub.monthly_verifications_limit} exceeded on {sub.tier} tier"
            ))
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Monthly verification quota of {sub.monthly_verifications_limit} exceeded on {sub.tier} tier. Please upgrade your subscription."
            )

    # Cache state in Redis for subsequent O(1) requests (TTL: 5 minutes)
    if not cached:
        await set_cached_subscription_state(app_id, {
            "tier": sub.tier,
            "status": sub.status,
            "expiry_date": sub.expiry_date.isoformat(),
            "monthly_verifications_limit": sub.monthly_verifications_limit,
            "rfid_enabled": sub.rfid_enabled,
            "log_retention_days": sub.log_retention_days,
            "realtime_alerts_enabled": sub.realtime_alerts_enabled
        }, ttl_seconds=300)

    return estate, sub

async def verify_rfid_access(db: AsyncSession, rfid_tag: str, app_id: str) -> dict:
    estate, sub = await _check_and_update_subscription(db, app_id, is_rfid=True)

    result = await db.execute(
        select(User).where(
            User.app_id == app_id,
            User.rfid_tag == rfid_tag,
            User.is_revoked == False
        )
    )
    user = result.scalar_one_or_none()
    
    if not user:
        db.add(AccessLog(
            app_id=app_id,
            event_type="rfid_verification",
            identifier=rfid_tag,
            status="denied",
            denial_reason="Invalid RFID tag or user revoked"
        ))
        await db.commit()
        return None
        
    # Extra check for resident hierarchy revocation
    if UserRole.RESIDENT in user.roles and user.landlord_id:
        land_result = await db.execute(select(User).where(User.id == user.landlord_id))
        landlord = land_result.scalar_one_or_none()
        if not landlord or landlord.is_revoked:
            db.add(AccessLog(
                app_id=app_id,
                event_type="rfid_verification",
                identifier=rfid_tag,
                status="denied",
                denial_reason="Associated landlord revoked or inactive",
                user_id=user.id
            ))
            await db.commit()
            return None

    # Access authorized
    if sub:
        sub.current_month_verifications += 1

    db.add(AccessLog(
        app_id=app_id,
        event_type="rfid_verification",
        identifier=rfid_tag,
        status="authorized",
        user_id=user.id
    ))
    await db.commit()

    # Real-time alert dispatch if enabled on the tier
    if sub and sub.realtime_alerts_enabled and user.email:
        asyncio.create_task(
            notification_service.send_realtime_access_alert(
                user.email,
                "RFID Gate Entry Authorized",
                f"Your RFID tag was authorized for gate entry at {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}."
            )
        )

    return {"status": "authorized", "user_id": str(user.id), "roles": user.roles}

async def verify_visitor_token(
    db: AsyncSession, 
    code: str, 
    app_id: str,
    direction: Optional[str] = None
) -> Optional[dict]:
    """
    Unified Single-Path Verification for Security Personnel.
    Security guards enter the 4-digit code into ONE single screen.
    The system automatically detects whether the code is for:
    - ENTRY (check_in)
    - EXIT (book_out)
    """
    if direction:
        direction = direction.lower().strip()
        if direction not in ["entry", "exit"]:
            direction = None

    code = code.upper().strip()
    estate, sub = await _check_and_update_subscription(db, app_id, is_rfid=False)
    now = datetime.now(UTC).replace(tzinfo=None)

    # -------------------------------------------------------------------------
    # Path A: BOOK-OUT (EXIT) Check (unless caller explicitly restricted to entry)
    # -------------------------------------------------------------------------
    if direction != "entry":
        # 1. Fast Path: Redis Book-Out Cache
        cached_bookout = await consume_cached_bookout_token(app_id, code)
        if cached_bookout:
            token_id = uuid.UUID(cached_bookout["token_id"])
            resident_id = uuid.UUID(cached_bookout["resident_id"])
            visitor_name = cached_bookout["visitor_name"]

            await db.execute(
                update(VisitorToken)
                .where(VisitorToken.id == token_id)
                .values(status="checked_out", checked_out_at=now)
            )
            if sub:
                sub.current_month_verifications += 1

            db.add(AccessLog(
                app_id=app_id,
                event_type="bookout_verification",
                identifier=code,
                status="authorized",
                resident_id=resident_id,
                visitor_name=visitor_name,
                details="Exit book-out authorized via Redis fresh cache (< 20m)"
            ))
            await db.commit()

            if sub and sub.realtime_alerts_enabled and resident_id:
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

        # 2. Database Path for Book-Out
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
            # Exit barrier / security terminal fallback: allow checkout via original token code
            alt_res = await db.execute(
                select(VisitorToken).where(
                    VisitorToken.code == code,
                    VisitorToken.app_id == app_id,
                    VisitorToken.status == "checked_in"
                )
            )
            bookout_token = alt_res.scalar_one_or_none()
        if bookout_token:
            bookout_token.status = "checked_out"
            bookout_token.checked_out_at = now
            if sub:
                sub.current_month_verifications += 1

            db.add(AccessLog(
                app_id=app_id,
                event_type="bookout_verification",
                identifier=code,
                status="authorized",
                resident_id=bookout_token.resident_id,
                visitor_name=bookout_token.visitor_name,
                details="Exit book-out authorized via PostgreSQL lookup"
            ))
            await db.commit()

            if sub and sub.realtime_alerts_enabled and bookout_token.resident_id:
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

    # -------------------------------------------------------------------------
    # Path B: ENTRY Check (unless caller explicitly restricted to exit)
    # -------------------------------------------------------------------------
    if direction != "exit":
        # 1. Fast Path: Redis Entry Token Cache
        cached_entry = await consume_cached_visitor_token(app_id, code)
        if cached_entry:
            token_id = uuid.UUID(cached_entry["id"])
            resident_id = uuid.UUID(cached_entry["resident_id"])
            visitor_name = cached_entry["visitor_name"]

            await db.execute(
                update(VisitorToken)
                .where(VisitorToken.id == token_id)
                .values(is_used=True, status="checked_in", checked_in_at=now)
            )
            if sub:
                sub.current_month_verifications += 1

            db.add(AccessLog(
                app_id=app_id,
                event_type="visitor_token_verification",
                identifier=code,
                status="authorized",
                resident_id=resident_id,
                visitor_name=visitor_name,
                details="Fast-path entry verification via Redis fresh cache (< 20m)"
            ))
            await db.commit()

            if sub and sub.realtime_alerts_enabled and resident_id:
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

        # 2. Database Path for Entry
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
            entry_token.is_used = True
            entry_token.status = "checked_in"
            entry_token.checked_in_at = now
            if sub:
                sub.current_month_verifications += 1

            db.add(AccessLog(
                app_id=app_id,
                event_type="visitor_token_verification",
                identifier=code,
                status="authorized",
                resident_id=entry_token.resident_id,
                visitor_name=entry_token.visitor_name,
                details="Standard entry verification via PostgreSQL lookup"
            ))
            await db.commit()

            if sub and sub.realtime_alerts_enabled and entry_token.resident_id:
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

    # -------------------------------------------------------------------------
    # Neither matched -> Access Denied
    # -------------------------------------------------------------------------
    db.add(AccessLog(
        app_id=app_id,
        event_type="visitor_token_verification",
        identifier=code,
        status="denied",
        denial_reason="Invalid, expired, or already used visitor token/book-out code"
    ))
    await db.commit()
    return None

async def verify_bookout_token(db: AsyncSession, code: str, app_id: str) -> Optional[dict]:
    """Alias for backwards compatibility and dedicated exit barrier hardware."""
    return await verify_visitor_token(db, code, app_id, direction="exit")
