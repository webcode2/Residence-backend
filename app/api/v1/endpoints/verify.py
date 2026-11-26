from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from app.core.database import get_db
from app.services import verification as verify_service
from app.dependencies import validate_tenant, check_roles
from app.models.user import UserRole
router = APIRouter()


async def _token_denial(db, app_id, code):
    reason = await verify_service.classify_denied_token(db, app_id, code)
    messages = {
        "token_not_found": "Token not found",
        "token_expired": "Access denied: token expired",
        "token_used": "Access denied: token already used",
        "token_claimed": "Access denied: token already used or being processed",
    }
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=messages.get(reason, "Access denied: invalid, expired, or already used code"),
        headers={"X-Residence-Verification-Result": reason},
    )


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
            detail="Access Denied: Invalid tag or revoked user/host / property owner"
        )
    await _notify_visitor_access(db, app_id, result)
    return result

@router.get("/token/{code}", tags=["profiling"])
async def verify_token(
    code: str,
    direction: Optional[str] = Query(None, description="Optional lane direction: 'entry', 'exit', or auto-detect if omitted"),
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY]))
):
    """Verify and consume an estate token, optionally limited to one lane."""
    result = await verify_service.verify_visitor_token(db, code, app_id, direction=direction)
    if not result:
        raise await _token_denial(db, app_id, code)

    await _notify_visitor_access(db, app_id, result)
    return result

@router.get("/book-out/{code}", tags=["profiling"])
async def verify_bookout(
    code: str,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY]))
):
    """Verify and consume a token at an exit barrier."""
    result = await verify_service.verify_bookout_token(db, code, app_id)
    if not result:
        raise await _token_denial(db, app_id, code)

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
        from app.services import notification_queue as notify_queue
        await notify_queue.dispatch(
            'push',
            token=resident.fcm_token,
            title='Visitor departure' if result['action'] == 'book_out' else 'Visitor arrival',
            body=result.get('message', 'Visitor access updated'),
            data={'type': 'visitor_access', 'action': result['action'], 'app_id': app_id, 'notification_count': await unread_count(db,resident.id,app_id)},
        )
