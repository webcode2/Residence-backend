from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.services import auth as auth_service
from app.schemas.user import TokenSchema, LoginRequestSchema, AuthResponseSchema
from app.models.user import User
from app.dependencies import validate_tenant, ValidateTenant, oauth2_scheme, get_current_user
from app.core.redis import check_login_blocked, record_login_failure, reset_login_failures
import uuid

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
    rate_key = f"{app_id}:{login_in.email}:{client_ip}"

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
        data={"sub": user.email, "app_id": app_id, "roles": [role.value for role in user.roles]}
    )

    # Pre-warm user profile in Redis so the user's first authenticated request is an instant cache hit
    from app.core.redis import set_cached_user
    from app.core.config import settings
    await set_cached_user(
        app_id=app_id,
        email=user.email,
        user_data={
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "hashed_password": user.hashed_password,
            "roles": [role.value for role in user.roles],
            "rfid_tag": user.rfid_tag,
            "house_number": user.house_number,
            "street_name": user.street_name,
            "is_revoked": user.is_revoked,
            "app_id": user.app_id,
            "landlord_id": str(user.landlord_id) if user.landlord_id else None
        },
        ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )

    return {
        "token": {"access_token": access_token, "token_type": "bearer"},
        "user": user
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
    from app.core.config import settings

    await blacklist_token(token, ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    await invalidate_cached_user(current_user.app_id, current_user.email)
    return {"status": "success", "message": "Successfully logged out and token invalidated"}


