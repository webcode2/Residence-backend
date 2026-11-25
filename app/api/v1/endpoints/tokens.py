from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.token import (
    RegistrationTokenSchema,
    VisitorTokenSchema,
    VisitorTokenCreateSchema,
    RegistrationTokenCreateSchema,
    BookOutTokenResponseSchema
)
from app.services import tokens as token_service
from app.dependencies import get_current_user, validate_tenant, check_roles
from app.models.user import User, UserRole
from app.core.redis import check_rate_limit
import uuid

router = APIRouter()

@router.post("/registration", response_model=RegistrationTokenSchema)
async def create_registration_token(
    token_in: RegistrationTokenCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Landlords invite a family member or tenant to their own household."""
    if UserRole.LANDLORD not in current_user.roles:
        raise HTTPException(status_code=403, detail="Only hosts can invite household members")
    if token_in.landlord_id is not None and token_in.landlord_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only invite members to your own household")

    # Rate limiting: 40 tokens / min for Caretakers
    is_allowed, count, retry_after = await check_rate_limit(
        key_prefix="token_create:registration",
        identifier=f"{app_id}:{current_user.id}",
        max_requests=40,
        window_seconds=60
    )
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: max 40 registration tokens per minute for caretakers. Please try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)}
        )
        
    return await token_service.create_registration_token(
        db, 
        landlord_id=current_user.id,
        app_id=app_id,
        house_number=token_in.house_number,
        street_name=token_in.street_name,
        relationship_type=token_in.relationship_type
    )

@router.post("/registration/{code}/revoke", response_model=dict)
async def revoke_registration_token(
    code: str,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Revoke a registration code if it was exposed or is no longer valid."""
    from sqlalchemy import select
    from app.models.token import RegistrationToken
    owned = (await db.execute(select(RegistrationToken).where(RegistrationToken.code == code,
        RegistrationToken.app_id == app_id, RegistrationToken.landlord_id == current_user.id))).scalar_one_or_none()
    if UserRole.CARETAKER not in current_user.roles and (UserRole.LANDLORD not in current_user.roles or not owned):
        raise HTTPException(status_code=403, detail="You can only revoke your own household invitations")

    success = await token_service.revoke_registration_token(db, code, app_id)
    if not success:
        raise HTTPException(status_code=404, detail="Token not found")
        
    return {"status": "success", "message": f"Token {code} has been revoked"}

@router.post("/visitor", response_model=VisitorTokenSchema)
async def create_visitor_token(
    token_in: VisitorTokenCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate codes for visitors.
    - Rate limits: 30 tokens/min for Residents & Landlords, 40 tokens/min for Caretakers.
    - Security guards cannot generate tokens unless they also have a resident role.
    """
    user_roles = set(current_user.roles)

    # Guards cannot generate tokens unless they also hold the resident role
    if UserRole.SECURITY in user_roles and UserRole.RESIDENT not in user_roles and UserRole.CARETAKER not in user_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Security guards cannot generate visitor tokens unless they also have a resident role"
        )

    allowed = {UserRole.RESIDENT, UserRole.LANDLORD, UserRole.CARETAKER}
    if not user_roles.intersection(allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to generate visitor codes"
        )

    # Rate limiting: 40 tokens/min for Caretakers, 30 tokens/min for Residents & Landlords
    if UserRole.CARETAKER in user_roles:
        max_tokens = 40
        role_label = "caretakers"
    else:
        max_tokens = 30
        role_label = "residents and hosts / property owners"

    is_allowed, count, retry_after = await check_rate_limit(
        key_prefix="token_create:visitor",
        identifier=f"{app_id}:{current_user.id}",
        max_requests=max_tokens,
        window_seconds=60
    )
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: max {max_tokens} visitor tokens per minute for {role_label}. Please try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)}
        )

    return await token_service.create_visitor_token(db, token_in, current_user.id, app_id)

@router.get("/visitor/active", response_model=List[VisitorTokenSchema])
async def get_active_visitors_roster(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.SECURITY, UserRole.CARETAKER]))
):
    """
    Roster of visitors currently checked in inside the estate.
    Used by security guards on their gate terminal to view on-premises visitors.
    """
    return await token_service.get_active_visitors(db, app_id)

@router.get("/visitor/history", response_model=List[VisitorTokenSchema])
async def visitor_history(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import select, or_
    from app.models.token import VisitorToken
    from app.core.redis import get_token_live_statuses, get_cached_visitor_history, set_cached_visitor_history
    from app.services.history_policy import retention
    from app.models.base import utc_now
    from datetime import datetime, timedelta
    staff = bool({UserRole.SECURITY, UserRole.CARETAKER}.intersection(current_user.roles))
    # Cache-aside for a resident's own list: Redis first; on a miss or after the TTL,
    # load from Postgres and re-cache. Staff see the whole estate, so they always read Postgres.
    rows = None if staff else await get_cached_visitor_history(app_id, str(current_user.id))
    if rows is None:
        days,_=await retention(db,app_id)
        cutoff=utc_now()-timedelta(days=days)
        query = select(VisitorToken).where(VisitorToken.app_id == app_id,or_(
            VisitorToken.status=='checked_in',VisitorToken.expires_at>=cutoff,VisitorToken.checked_out_at>=cutoff))
        if not staff:
            query = query.where(VisitorToken.resident_id == current_user.id)
        records = (await db.execute(query.order_by(VisitorToken.created_at.desc()).limit(200))).scalars().all()
        rows = [VisitorTokenSchema.model_validate(r).model_dump(mode="json") for r in records]
        if not staff:
            await set_cached_visitor_history(app_id, str(current_user.id), rows)
    # Gate check-in/out lands in Redis first; overlay it for every row in one MGET.
    live_by_id = await get_token_live_statuses([row["id"] for row in rows])
    result = []
    for row in rows:
        item = VisitorTokenSchema.model_validate(row)
        live = live_by_id.get(str(item.id))
        if live:
            item.status = live.get("status", item.status)
            for field in ("checked_in_at", "checked_out_at"):
                if live.get(field):
                    setattr(item, field, datetime.fromisoformat(live[field]))
        if staff and item.resident_id != current_user.id:
            # A generated passcode is private to the resident who made it: staff only see visitors
            # who actually came through the gate, and never the entry or book-out codes.
            if item.status == 'pending':
                continue
            item.code, item.bookout_code = '', None
        result.append(item)
    return result

@router.post("/visitor/{token_id}/book-out", response_model=BookOutTokenResponseSchema)
async def generate_bookout_token(
    token_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate an estate-configured book-out code for a checked-in visitor.
    - Rate limits: 30 tokens/min for Residents & Landlords, 40 tokens/min for Caretakers.
    - Security guards cannot generate tokens unless they also have a resident role.
    """
    user_roles = set(current_user.roles)

    # Guards cannot generate bookout tokens unless they also hold the resident role
    if UserRole.SECURITY in user_roles and UserRole.RESIDENT not in user_roles and UserRole.CARETAKER not in user_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Security guards cannot generate book-out tokens unless they also have a resident role"
        )

    allowed = {UserRole.RESIDENT, UserRole.LANDLORD, UserRole.CARETAKER}
    if not user_roles.intersection(allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to generate book-out tokens"
        )

    # Rate limiting: 40 tokens/min for Caretakers, 30 tokens/min for Residents & Landlords
    if UserRole.CARETAKER in user_roles:
        max_tokens = 40
        role_label = "caretakers"
    else:
        max_tokens = 30
        role_label = "residents and hosts / property owners"

    is_allowed, count, retry_after = await check_rate_limit(
        key_prefix="token_create:bookout",
        identifier=f"{app_id}:{current_user.id}",
        max_requests=max_tokens,
        window_seconds=60
    )
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: max {max_tokens} book-out tokens per minute for {role_label}. Please try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)}
        )

    can_manage_all = bool({UserRole.CARETAKER, UserRole.SECURITY}.intersection(user_roles))
    return await token_service.create_bookout_token(
        db=db,
        token_id=token_id,
        resident_id=current_user.id,
        app_id=app_id,
        can_manage_all=can_manage_all
    )



@router.post("/visitor/{token_id}/checkout", response_model=dict)
async def direct_security_checkout(
    token_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.SECURITY]))
):
    """
    Direct one-tap book out by security personnel at the exit gate terminal.
    Immediately marks the visitor as checked out without requiring code generation.
    """
    result = await token_service.direct_security_checkout(
        db=db,
        token_id=token_id,
        app_id=app_id,
        guard_id=current_user.id
    )

    from app.api.v1.endpoints.verify import _notify_visitor_access
    await _notify_visitor_access(db, app_id, result)
    return result

@router.get('/registration', response_model=List[RegistrationTokenSchema])
async def list_household_invitations(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.LANDLORD]))):
    from sqlalchemy import select
    from app.models.token import RegistrationToken
    return (await db.execute(select(RegistrationToken).where(RegistrationToken.app_id == app_id,
        RegistrationToken.landlord_id == current_user.id, RegistrationToken.is_revoked.is_(False))
        .order_by(RegistrationToken.created_at.desc()).limit(100))).scalars().all()
