from typing import Optional, Dict, Any, Tuple, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from app.models.token import RegistrationToken, VisitorToken
from app.models.estate import Estate, Subscription
from app.models.user import User
from app.services.notifications import notification_service
from app.schemas.token import VisitorTokenCreateSchema, BookOutTokenResponseSchema
from app.core.redis import (
    cache_visitor_token,
    cache_registration_token,
    invalidate_cached_registration_token,
    cache_bookout_token,
    consume_cached_bookout_token,
    get_token_live_status,
    is_token_code_claimed,
)
from datetime import datetime, timedelta, date, UTC
from fastapi import HTTPException, status
import asyncio
import secrets
import string
import uuid

def generate_registration_code() -> str:
    # 9-char alphanumeric code (e.g. REG4N8P3Q)
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"REG{suffix}"

VISITOR_TOKEN_CHARSET = string.ascii_uppercase  # A-Z uppercase letters

def generate_visitor_code(length: int = 6) -> str:
    """Generates an uppercase A-Z code of specified length (default 6, or 8)."""
    return ''.join(secrets.choice(VISITOR_TOKEN_CHARSET) for _ in range(length))

async def get_estate_token_code_length(db: AsyncSession, app_id: str) -> int:
    """Fetch the configured token code length for the estate (default: 6, or 8)."""
    result = await db.execute(select(Estate.token_code_length).where(Estate.app_id == app_id))
    length = result.scalar_one_or_none()
    return length if length in [6, 8] else 6

async def is_visitor_code_active(db: AsyncSession, app_id: str, candidate_code: str) -> bool:
    """
    Checks if candidate_code is currently in use in this estate by:
    1. Any active entry visitor token (status in ['pending', 'checked_in'] and not expired).
    2. Any active book-out token (status == 'checked_in', bookout_expires_at > now).
    3. Any code recently claimed at the gate (Postgres write may still be queued).
    Enforces cross-namespace uniqueness so no active entry code ever matches an active exit code!
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    clean_code = candidate_code.upper().strip()

    if await is_token_code_claimed(app_id, clean_code):
        return True

    # Check active entry tokens (pending or checked_in)
    entry_stmt = select(VisitorToken.id).where(
        VisitorToken.app_id == app_id,
        VisitorToken.code == clean_code,
        VisitorToken.status.in_(["pending", "checked_in"]),
        VisitorToken.expires_at > now
    ).limit(1)
    if (await db.execute(entry_stmt)).scalar_one_or_none():
        return True

    # Check active book-out tokens
    bookout_stmt = select(VisitorToken.id).where(
        VisitorToken.app_id == app_id,
        VisitorToken.bookout_code == clean_code,
        VisitorToken.status == "checked_in",
        VisitorToken.bookout_expires_at > now
    ).limit(1)
    if (await db.execute(bookout_stmt)).scalar_one_or_none():
        return True

    return False

async def generate_unique_visitor_code(
    db: AsyncSession, 
    app_id: str, 
    length: Optional[int] = None, 
    max_retries: int = 30
) -> str:
    """
    Generates an uppercase A-Z code (default 6 characters, or 8 if configured on dashboard)
    guaranteed to be unique across both active entry and active exit codes in the estate.
    """
    if length is None:
        length = await get_estate_token_code_length(db, app_id)

    for _ in range(max_retries):
        candidate = generate_visitor_code(length=length)
        if not await is_visitor_code_active(db, app_id, candidate):
            return candidate

    # High-density fallback: add 1 char
    for _ in range(10):
        candidate = generate_visitor_code(length=length + 1)
        if not await is_visitor_code_active(db, app_id, candidate):
            return candidate

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Unable to allocate unique code at this time. Please retry."
    )

async def _validate_subscription_for_tokens(
    db: AsyncSession, 
    app_id: str, 
    is_visitor_token: bool = False
) -> Subscription | None:
    """Validate estate subscription state and quotas before generating tokens."""
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    if not estate or not estate.subscription:
        return None

    sub = estate.subscription
    today = date.today()

    # 1. Check active status
    if sub.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Estate subscription is {sub.status}. Token generation is disabled."
        )

    # 2. Check expiration
    if sub.expiry_date < today:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Estate subscription has expired. Token generation is disabled."
        )

    # 3. Check monthly billing cycle reset
    if (today - sub.billing_cycle_start).days >= 30:
        sub.current_month_verifications = 0
        sub.billing_cycle_start = today
        from app.core.redis import reset_monthly_quota_redis
        await reset_monthly_quota_redis(app_id)

    # 4. For visitor tokens, check if monthly quota is already exhausted (Redis-first)
    if is_visitor_token and sub.monthly_verifications_limit is not None:
        from app.core.redis import get_monthly_quota_redis
        used = await get_monthly_quota_redis(app_id)
        if used is None:
            used = sub.current_month_verifications
        if used >= sub.monthly_verifications_limit:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Monthly verification quota ({sub.monthly_verifications_limit}) reached on {sub.tier} tier. Upgrade your plan to generate more visitor tokens."
            )

    return sub

async def create_registration_token(
    db: AsyncSession, 
    landlord_id: uuid.UUID, 
    app_id: str, 
    house_number: str = None, 
    street_name: str = None
) -> RegistrationToken:
    await _validate_subscription_for_tokens(db, app_id, is_visitor_token=False)

    db_obj = RegistrationToken(
        code=generate_registration_code(),
        landlord_id=landlord_id,
        house_number=house_number,
        street_name=street_name,
        app_id=app_id
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)

    # Cache in Redis with 20-minute TTL for fast self-registration
    await cache_registration_token(
        app_id=app_id,
        code=db_obj.code,
        token_data={
            "id": str(db_obj.id),
            "code": db_obj.code,
            "landlord_id": str(db_obj.landlord_id),
            "app_id": app_id,
            "house_number": db_obj.house_number,
            "street_name": db_obj.street_name
        },
        ttl_seconds=1200 # 20 minutes
    )

    return db_obj

async def revoke_registration_token(db: AsyncSession, code: str, app_id: str) -> bool:
    """Revoke a registration token so it cannot be used anymore."""
    result = await db.execute(
        update(RegistrationToken)
        .where(RegistrationToken.code == code, RegistrationToken.app_id == app_id)
        .values(is_revoked=True)
    )
    await db.commit()
    await invalidate_cached_registration_token(app_id, code)
    return result.rowcount > 0

async def create_visitor_token(
    db: AsyncSession, 
    token_in: VisitorTokenCreateSchema, 
    resident_id: uuid.UUID, 
    app_id: str
) -> VisitorToken:
    await _validate_subscription_for_tokens(db, app_id, is_visitor_token=True)

    code = await generate_unique_visitor_code(db, app_id)
    db_obj = VisitorToken(
        code=code,
        visitor_name=token_in.visitor_name,
        resident_id=resident_id,
        app_id=app_id,
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24) # Valid for 24h
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)

    # Cache in Redis with 20-minute TTL for instant gate verification
    await cache_visitor_token(
        app_id=app_id,
        code=db_obj.code,
        token_data={
            "id": str(db_obj.id),
            "code": db_obj.code,
            "visitor_name": db_obj.visitor_name,
            "resident_id": str(db_obj.resident_id),
            "app_id": app_id,
            "expires_at": db_obj.expires_at.isoformat()
        },
        ttl_seconds=1200 # 20 minutes
    )

    return db_obj

async def create_bookout_token(
    db: AsyncSession,
    token_id: uuid.UUID,
    resident_id: uuid.UUID,
    app_id: str,
    can_manage_all: bool = False
) -> dict:
    """
    Generate a fresh book-out (exit) token for a specific checked-in visitor pass.
    Can be generated by the host resident, or directly by security / caretaker.
    """
    await _validate_subscription_for_tokens(db, app_id, is_visitor_token=True)

    result = await db.execute(
        select(VisitorToken).where(
            VisitorToken.id == token_id,
            VisitorToken.app_id == app_id
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Visitor token not found"
        )

    # Permission check: resident can only book out their own visitors (security & caretaker can manage all)
    if not can_manage_all and token.resident_id != resident_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: you can only generate book-out tokens for your own visitors"
        )

    # State validation — honor live Redis status (check-in may still be queued for Postgres)
    live = await get_token_live_status(str(token.id))
    effective_status = (live or {}).get("status") or token.status

    if effective_status == "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot book out: visitor has not checked in at the entry gate yet"
        )
    if effective_status == "checked_out":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Visitor has already checked out of the estate"
        )

    # Generate fresh 4-digit exit code with 2 hours validity, guaranteed unique across entry & exit
    bookout_code = await generate_unique_visitor_code(db, app_id)
    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)

    token.bookout_code = bookout_code
    token.bookout_expires_at = expires_at
    await db.commit()
    await db.refresh(token)

    # Cache in Redis with 20-minute TTL for fast exit gate reader
    await cache_bookout_token(
        app_id=app_id,
        code=bookout_code,
        token_data={
            "token_id": str(token.id),
            "visitor_name": token.visitor_name,
            "resident_id": str(token.resident_id),
            "app_id": app_id,
            "bookout_code": bookout_code,
            "checked_in_at": token.checked_in_at.isoformat() if token.checked_in_at else None,
            "expires_at": expires_at.isoformat()
        },
        ttl_seconds=1200 # 20 minutes
    )

    return {
        "token_id": token.id,
        "visitor_name": token.visitor_name,
        "bookout_code": bookout_code,
        "status": effective_status,
        "expires_at": expires_at,
        "message": "Present this 4-digit code at the exit gate to book out."
    }

async def direct_security_checkout(
    db: AsyncSession,
    token_id: uuid.UUID,
    app_id: str,
    guard_id: uuid.UUID
) -> dict:
    """
    Direct checkout performed by security personnel at the gate terminal.
    Immediately transitions visitor status from checked_in -> checked_out.
    """
    await _validate_subscription_for_tokens(db, app_id, is_visitor_token=True)

    result = await db.execute(
        select(VisitorToken).where(
            VisitorToken.id == token_id,
            VisitorToken.app_id == app_id
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Visitor token not found"
        )

    live = await get_token_live_status(str(token.id))
    effective_status = (live or {}).get("status") or token.status

    if effective_status == "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot book out: visitor has not checked in at the entry gate yet"
        )
    if effective_status == "checked_out":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Visitor has already checked out of the estate"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    # Queue Postgres write via live status + enqueue pattern used by gate verify
    from app.services.token_state import persist_token_check_out
    await persist_token_check_out(
        db,
        token_id=token.id,
        app_id=app_id,
        code=token.bookout_code or token.code,
        at=now,
        already_claimed=True,
    )

    # Invalidate any cached bookout code in redis
    if token.bookout_code:
        await consume_cached_bookout_token(app_id, token.bookout_code)

    from app.services.access_logs import record_access_event
    await record_access_event(
        db,
        app_id=app_id,
        event_type="bookout_verification",
        identifier=token.code,
        status="authorized",
        resident_id=token.resident_id,
        visitor_name=token.visitor_name,
        details=f"Direct book-out checkout by security personnel ({guard_id})",
    )

    # Real-time departure alert to host resident
    res_result = await db.execute(select(User).where(User.id == token.resident_id))
    resident = res_result.scalar_one_or_none()
    if resident and resident.email:
        asyncio.create_task(
            notification_service.send_realtime_access_alert(
                resident.email,
                f"Visitor Departed: {token.visitor_name}",
                f"Security has checked out your visitor '{token.visitor_name}' at the exit gate at {now.strftime('%Y-%m-%d %H:%M:%S UTC')}."
            )
        )

    return {
        "status": "authorized",
        "action": "book_out",
        "token_id": token.id,
        "visitor_name": token.visitor_name,
        "resident_id": token.resident_id,
        "checked_in_at": token.checked_in_at or (live or {}).get("checked_in_at"),
        "checked_out_at": now,
        "message": f"Visitor {token.visitor_name} successfully booked out by security"
    }

async def get_active_visitors(db: AsyncSession, app_id: str) -> list[VisitorToken]:
    """
    Returns all visitors currently inside the estate (status == 'checked_in').
    Empowers security personnel with a real-time on-premises visitor roster.
    """
    result = await db.execute(
        select(VisitorToken)
        .where(
            VisitorToken.app_id == app_id,
            VisitorToken.status == "checked_in"
        )
        .order_by(VisitorToken.checked_in_at.desc())
    )
    return list(result.scalars().all())
