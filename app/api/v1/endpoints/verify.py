from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Literal, Optional
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

@router.get("/token-preview/{code}", tags=["profiling"])
async def preview_token(
    code: str,
    response: Response,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY])),
):
    """Show who the guest is visiting before the guard approves access."""
    result = await verify_service.preview_visitor_token(db, code, app_id)
    if result is None:
        denial = await _token_denial(db, app_id, code)
        denial.headers["Cache-Control"] = "no-store"
        raise denial
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/token/{code}/approve", tags=["profiling"])
async def approve_token(
    code: str,
    direction: Literal["entry", "exit"] = Query(...),
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.SECURITY])),
):
    """Record the access action explicitly approved by the guard."""
    preview = await verify_service.preview_visitor_token(db, code, app_id)
    if preview is None:
        raise await _token_denial(db, app_id, code)
    expected = "check_in" if direction == "entry" else "book_out"
    if preview["action"] != expected:
        raise HTTPException(409, "Token action changed. Look up the token again.")
    result = await verify_token(code, direction, app_id, db, current_user)
    return {**result, "host_name": preview["host_name"]}


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
    """Save the resident's inbox entry and queue a push for successful access."""
    import uuid
    from sqlalchemy import select
    from app.models.user import User
    from app.services import notification_queue
    from app.services.notification_inbox import record, unread_count

    action = result.get('action')
    if action not in ('check_in', 'book_out') or not result.get('resident_id'):
        return
    resident = (await db.execute(select(User).where(
        User.id == uuid.UUID(str(result['resident_id'])), User.app_id == app_id,
        User.is_revoked.is_(False),
    ))).scalar_one_or_none()
    if resident is None:
        return

    visitor = (result.get('visitor_name') or '').strip() or 'Your visitor'
    if action == 'book_out':
        title = 'Visitor departure'
        body = f'{visitor} checked out at the gate.'
    else:
        title = 'Visitor arrival'
        body = f'{visitor} checked in at the gate.'

    event_key = f'access:{uuid.uuid4()}'
    data = {
        'type': 'visitor_access', 'action': action, 'app_id': app_id,
        'notification_event_key': event_key,
    }
    await record(db, resident.id, app_id, event_key, title, body, data)
    await db.commit()
    if resident.fcm_token:
        await notification_queue.dispatch(
            'push',
            token=resident.fcm_token,
            title=title,
            body=body,
            data={**data, 'notification_count': await unread_count(db, resident.id, app_id)},
        )
