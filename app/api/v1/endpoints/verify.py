from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from app.core.database import get_db
from app.services import verification as verify_service
from app.dependencies import validate_tenant, check_roles
from app.models.user import UserRole
from app.core.redis import (
    check_verification_blocked,
    record_verification_failure,
    reset_verification_failures
)

router = APIRouter()

def _get_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

@router.get("/rfid/{rfid_tag}", tags=["profiling"])
async def verify_rfid(
    rfid_tag: str,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY]))
):
    result = await verify_service.verify_rfid_access(db, rfid_tag, app_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access Denied: Invalid tag or revoked user/landlord"
        )
    await _notify_visitor_access(db, app_id, result)
    return result

@router.get("/token/{code}", tags=["profiling"])
async def verify_token(
    code: str,
    request: Request,
    direction: Optional[str] = Query(None, description="Optional lane direction: 'entry', 'exit', or auto-detect if omitted"),
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY]))
):
    """
    Universal Verification Endpoint for physical gate security.
    Security personnel enter ANY 4-digit code into a SINGLE screen.
    - If direction is passed ('entry' or 'exit'), strict lane filtering is applied.
    - Protected by Redis rate-limiting (5 consecutive failures -> 60s cooldown).
    """
    client_ip = _get_client_ip(request)
    code = code.upper().strip()

    # 1. Anti-brute-force check: Is this client/gate terminal currently blocked?
    is_blocked, remaining = await check_verification_blocked(app_id, client_ip)
    if is_blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed verification attempts. Gate terminal temporarily blocked for {remaining} seconds."
        )

    # 2. Verify token (with optional lane direction)
    result = await verify_service.verify_visitor_token(db, code, app_id, direction=direction)
    if not result:
        # Record failure and check if block threshold reached
        failures, is_now_blocked, block_secs = await record_verification_failure(
            app_id, client_ip, max_failures=5, window_seconds=60, block_seconds=60
        )
        if is_now_blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed verification attempts (5). Gate terminal temporarily blocked for {block_secs} seconds."
            )
        attempts_left = max(0, 5 - failures)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Access Denied: Invalid, expired, or already used code. ({attempts_left} attempts remaining before temporary cooldown)"
        )

    # 3. Successful verification: Reset failure counter
    await reset_verification_failures(app_id, client_ip)
    await _notify_visitor_access(db, app_id, result)
    return result

@router.get("/book-out/{code}", tags=["profiling"])
async def verify_bookout(
    code: str,
    request: Request,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY]))
):
    """
    Dedicated exit barrier verification endpoint (or security personnel can 
    use the universal /token/{code}?direction=exit endpoint on their main screen).
    """
    client_ip = _get_client_ip(request)
    code = code.upper().strip()

    # Anti-brute-force check
    is_blocked, remaining = await check_verification_blocked(app_id, client_ip)
    if is_blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed verification attempts. Gate terminal temporarily blocked for {remaining} seconds."
        )

    result = await verify_service.verify_bookout_token(db, code, app_id)
    if not result:
        failures, is_now_blocked, block_secs = await record_verification_failure(
            app_id, client_ip, max_failures=5, window_seconds=60, block_seconds=60
        )
        if is_now_blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed verification attempts (5). Gate terminal temporarily blocked for {block_secs} seconds."
            )
        attempts_left = max(0, 5 - failures)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Access Denied: Invalid, expired, or unapproved book-out code. ({attempts_left} attempts remaining before temporary cooldown)"
        )

    await reset_verification_failures(app_id, client_ip)
    await _notify_visitor_access(db, app_id, result)
    return result


async def _notify_visitor_access(db, app_id, result):
    """Dispatch a push after successful verification; no client polling needed."""
    import asyncio
    import uuid
    from sqlalchemy import select
    from app.models.user import User
    from app.services.fcm import fcm_service
    if result.get('action') not in ('check_in', 'book_out') or not result.get('resident_id'):
        return
    resident = (await db.execute(select(User).where(
        User.id == uuid.UUID(str(result['resident_id'])), User.app_id == app_id,
        User.is_revoked.is_(False),
    ))).scalar_one_or_none()
    if resident:
        from app.services.notification_inbox import record, unread_count
        notification_id=str(uuid.uuid4())
        await record(db,resident.id,app_id,'access:'+notification_id,'Visitor departure' if result['action']=='book_out' else 'Visitor arrival',result.get('message','Visitor access updated'),{'type':'visitor_access','action':result['action'],'app_id':app_id})
        await db.commit()
    if resident and resident.fcm_token:
        asyncio.create_task(fcm_service.send_to_token(
            resident.fcm_token,
            'Visitor departure' if result['action'] == 'book_out' else 'Visitor arrival',
            result.get('message', 'Visitor access updated'),
            data={'type': 'visitor_access', 'action': result['action'], 'app_id': app_id, 'notification_count': await unread_count(db,resident.id,app_id)},
        ))
