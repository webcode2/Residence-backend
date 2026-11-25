from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.services import auth as auth_service
from app.schemas.user import ChangePasswordSchema, TokenSchema, LoginRequestSchema, AuthResponseSchema
from app.models.user import User
from app.dependencies import validate_tenant, ValidateTenant, oauth2_scheme, get_current_user, get_authenticated_user
from app.core.redis import check_login_blocked, record_login_failure, reset_login_failures
import uuid
from datetime import timedelta, datetime, UTC
from app.core.config import settings

def _remaining_token_seconds(token: str) -> int:
    from jose import jwt
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    return max(1, int(claims["exp"] - datetime.now(UTC).timestamp()) + 1)

router = APIRouter()

@router.post("/login", response_model=AuthResponseSchema)
async def login_for_access_token(
    login_in: LoginRequestSchema,
    request: Request,
    app_id: str = Depends(ValidateTenant(is_auth_route=True)),
    db: AsyncSession = Depends(get_db)
):
    """
    Login with JSON body.
    Protected with anti-brute-force rate limiting.
    Requires X-App-Id header for tenant identification.
    """
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{app_id}:{login_in.email.strip().lower()}:{client_ip}"  # one lockout for every capitalisation

    # 1. Check if temporarily blocked
    is_blocked, remaining = await check_login_blocked(rate_key)
    if is_blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Please try again in {remaining} seconds."
        )

    user = await auth_service.authenticate_user(
        db, email=login_in.email, password=login_in.password, app_id=app_id
    )
    
    if not user:
        # Record failure (5 failures -> 60s cooldown)
        _, now_blocked, block_secs = await record_login_failure(rate_key)
        if now_blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed login attempts. Cooldown active for {block_secs} seconds."
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reset failure counter on success
    await reset_login_failures(rate_key)

    
    if user.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is revoked",
        )

    access_token = auth_service.create_access_token(
        data={"sub": user.email, "uid": str(user.id), "app_id": app_id, "roles": [role.value for role in user.roles], "purpose": "password_change" if user.must_change_password else "access", "jti": str(uuid.uuid4())},
        expires_delta=timedelta(minutes=30) if user.must_change_password else timedelta(days=settings.TENANT_SESSION_DAYS)
    )

    # Pre-warm user profile in Redis so the user's first authenticated request is an instant cache hit
    from app.core.redis import set_cached_user, user_cache_payload
    await set_cached_user(
        app_id=app_id,
        email=user.email,
        user_data=user_cache_payload(user),
        ttl_seconds=1800
    )

    # Include estate code length for the mobile settings cache and guard input.
    from app.models.estate import Estate
    from sqlalchemy import select as sa_select
    estate_result = await db.execute(sa_select(Estate.token_code_length).where(Estate.app_id == app_id))
    token_code_length = estate_result.scalar_one_or_none() or 6

    return {
        "token": {"access_token": access_token, "token_type": "bearer"},
        "user": user,
        "token_code_length": token_code_length,
    }

@router.post("/logout", tags=["auth"])
async def logout(
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user)
):
    """
    Logout user, blacklist active JWT in Redis, and clear user session cache.
    """
    from app.core.redis import blacklist_token, invalidate_cached_user

    await blacklist_token(token, ttl_seconds=_remaining_token_seconds(token))
    await invalidate_cached_user(current_user.app_id, current_user.email)
    return {"status": "success", "message": "Successfully logged out and token invalidated"}



@router.post("/change-password", response_model=AuthResponseSchema)
async def change_password(
    body: ChangePasswordSchema,
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    from app.core.redis import invalidate_cached_user, blacklist_token
    user = await db.get(User, current_user.id, populate_existing=True)
    if not user or not await auth_service.verify_password_async(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    auth_service.require_acceptable_password(body.new_password)
    if await auth_service.verify_password_async(body.new_password, user.hashed_password):
        raise HTTPException(status_code=422, detail="Choose a different password")
    user.hashed_password = await auth_service.get_password_hash_async(body.new_password)
    user.must_change_password = False
    await db.commit()
    await db.refresh(user)
    await invalidate_cached_user(user.app_id, user.email)
    await blacklist_token(token, ttl_seconds=_remaining_token_seconds(token))
    fresh = auth_service.create_access_token({"sub": user.email, "uid": str(user.id), "app_id": user.app_id,
        "roles": [r.value for r in user.roles], "purpose": "access", "jti": str(uuid.uuid4())}, expires_delta=timedelta(days=settings.TENANT_SESSION_DAYS))
    from app.models.estate import Estate
    from sqlalchemy import select
    length = (await db.execute(select(Estate.token_code_length).where(Estate.app_id == user.app_id))).scalar_one_or_none() or 6
    return {"token": {"access_token": fresh, "token_type": "bearer"}, "user": user, "token_code_length": length}
