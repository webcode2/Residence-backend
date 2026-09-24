from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.user import User, UserRole
from app.schemas.user import LoginRequestSchema, AuthResponseSchema
from app.schemas.admin import AdminChangePasswordSchema, AdminProfileSchema
from app.services.auth import (
    verify_saas_admin_password,
    get_saas_admin_password_hash,
    create_saas_admin_access_token,
)
from app.dependencies import oauth2_scheme, get_current_saas_admin
from app.core.redis import (
    set_cached_user,
    blacklist_token,
    invalidate_cached_user,
    check_login_blocked,
    record_login_failure,
    reset_login_failures
)
from app.core.config import settings

router = APIRouter()

@router.post("/login", response_model=AuthResponseSchema, tags=["saas_admin_auth"])
async def admin_login(
    login_in: LoginRequestSchema,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Dedicated SaaS Platform Admin Authentication.
    - Verified using dedicated HMAC password pepper (isolated from tenant user hashes).
    - Issues JWT signed with SAAS_ADMIN_SECRET_KEY (isolated from tenant user secret).
    - Protected with anti-brute-force rate limiting.
    - Does NOT require X-App-Id header.
    """
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"admin:{login_in.email}:{client_ip}"

    # 1. Check if blocked
    is_blocked, remaining = await check_login_blocked(rate_key)
    if is_blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed admin login attempts. Please try again in {remaining} seconds."
        )

    result = await db.execute(select(User).where(User.email == login_in.email))
    user = result.scalar_one_or_none()

    if not user or not verify_saas_admin_password(login_in.password, user.hashed_password):
        _, now_blocked, block_secs = await record_login_failure(rate_key)
        if now_blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed admin login attempts. Account temporarily locked for {block_secs} seconds."
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect admin email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reset failure counter on success
    await reset_login_failures(rate_key)

    if user.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin account is revoked",
        )


    if UserRole.SAAS_OWNER not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Account does not have SaaS Admin (saas_owner) privileges",
        )

    access_token = create_saas_admin_access_token(
        data={"sub": user.email, "app_id": user.app_id, "roles": [role.value for role in user.roles]}
    )

    # Pre-warm Redis cache
    await set_cached_user(
        app_id=user.app_id,
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

@router.post("/logout", tags=["saas_admin_auth"])
async def admin_logout(
    token: str = Depends(oauth2_scheme),
    admin: User = Depends(get_current_saas_admin)
):
    """Logs out SaaS Admin, blacklists JWT in Redis, and clears user cache."""
    await blacklist_token(token, ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    await invalidate_cached_user(admin.app_id, admin.email)
    return {"status": "success", "message": "SaaS Admin logged out successfully"}

@router.get("/me", response_model=AdminProfileSchema, tags=["saas_admin_auth"])
async def get_admin_profile(admin: User = Depends(get_current_saas_admin)):
    """Fetch authenticated SaaS Admin profile."""
    return {
        "id": admin.id,
        "email": admin.email,
        "full_name": admin.full_name,
        "roles": [r.value for r in admin.roles],
        "app_id": admin.app_id,
        "created_at": admin.created_at
    }

@router.post("/change-password", tags=["saas_admin_auth"])
async def admin_change_password(
    pwd_in: AdminChangePasswordSchema,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """Change SaaS Admin password using dedicated peppered hashing."""
    if not verify_saas_admin_password(pwd_in.current_password, admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect current password"
        )

    admin.hashed_password = get_saas_admin_password_hash(pwd_in.new_password)
    await db.commit()
    await db.refresh(admin)

    # Invalidate cache
    await invalidate_cached_user(admin.app_id, admin.email)

    return {"status": "success", "message": "SaaS Admin password changed successfully"}
