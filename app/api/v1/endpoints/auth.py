from pydantic import BaseModel, EmailStr, Field
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
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

    # Serialize concurrent logins and push registration for this account.
    authenticated_password = user.hashed_password
    user = await db.scalar(select(User).where(User.id == user.id).with_for_update()
                           .execution_options(populate_existing=True))
    if user is None or user.hashed_password != authenticated_password:
        raise HTTPException(401, "Credentials changed. Sign in again.")

    # Reset failure counter on success
    await reset_login_failures(rate_key)


    if user.is_revoked:
        # Only reached with the correct password, so this reveals nothing to guessers.
        # The header tells the app to offer "Request account reinstatement".
        from app.services.account_archive import reinstatement_eligible
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is revoked",
            headers={"X-Account-Status": "suspended"} if reinstatement_eligible(user) else None,
        )

    return await _start_session(db, user, app_id)


async def _start_session(db: AsyncSession, user: User, app_id: str) -> dict:
    """Make this the account's only session (one per account) and return the sign-in response."""
    active_session = uuid.uuid4().hex
    user.active_session_id = active_session
    await auth_service.clear_session_devices(db, user)
    await db.commit()
    await db.refresh(user)

    access_token = auth_service.create_access_token(
        data={"sub": user.email, "uid": str(user.id), "app_id": app_id, "roles": [role.value for role in user.roles], "purpose": "password_change" if user.must_change_password else "access", "sid": active_session, "jti": str(uuid.uuid4())},
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


class DeviceKeyEnrolRequest(BaseModel):
    device_label: str | None = Field(None, max_length=100)


class DeviceKeyLoginRequest(BaseModel):
    key_id: uuid.UUID
    secret: str = Field(min_length=20, max_length=200)


@router.post("/device-keys", status_code=201)
async def enrol_device_key(
    body: DeviceKeyEnrolRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn on fingerprint / Face ID sign-in on this phone (residents only).

    The secret is returned once; the app keeps it in secure storage behind a biometric check.
    """
    from app.services import device_keys

    user = await db.get(User, current_user.id, with_for_update=True, populate_existing=True)
    if user is None or not device_keys.eligible(user):
        raise HTTPException(403, "Fingerprint sign-in is available on resident accounts only")
    key, secret = await device_keys.enrol(db, user, body.device_label)
    await db.commit()
    return {"key_id": str(key.id), "secret": secret}


@router.delete("/device-keys/{key_id}", status_code=204)
async def revoke_device_key(
    key_id: uuid.UUID,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn off fingerprint sign-in for this phone."""
    from app.services import device_keys

    await device_keys.revoke(db, current_user, key_id)
    await db.commit()


@router.post("/device-login", response_model=AuthResponseSchema)
async def device_login(
    body: DeviceKeyLoginRequest,
    request: Request,
    app_id: str = Depends(ValidateTenant(is_auth_route=True)),
    db: AsyncSession = Depends(get_db),
):
    """Sign in with the phone's fingerprint sign-in key. Replaces any other session."""
    from app.services import device_keys

    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{app_id}:device:{body.key_id}:{client_ip}"
    is_blocked, remaining = await check_login_blocked(rate_key)
    if is_blocked:
        raise HTTPException(429, f"Too many failed attempts. Please try again in {remaining} seconds.")
    user = await device_keys.authenticate(db, app_id, body.key_id, body.secret)
    if user is None:
        await record_login_failure(rate_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Fingerprint sign-in is no longer set up for this account. Sign in with your password.",
            headers={"X-Device-Key": "invalid"},
        )
    await reset_login_failures(rate_key)
    return await _start_session(db, user, app_id)


class ReinstatementRequest(BaseModel):
    email: EmailStr


@router.post("/request-reinstatement", status_code=202)
async def request_reinstatement(
    body: ReinstatementRequest,
    request: Request,
    app_id: str = Depends(ValidateTenant(is_auth_route=True)),
    db: AsyncSession = Depends(get_db),
):
    from app.core.redis import check_rate_limit
    from app.services.account_archive import request_reinstatement

    client_ip = request.client.host if request.client else "unknown"
    allowed, _, _ = await check_rate_limit("archive_request", f"{app_id}:{client_ip}", 10, 3600)
    if allowed:
        await request_reinstatement(db, app_id, str(body.email))
    return {"message": "If the account is eligible, your estate management team will receive your request."}


@router.post("/logout", tags=["auth"])
async def logout(
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Logout user, blacklist active JWT in Redis, and clear user session cache.
    """
    from app.core.redis import blacklist_token, invalidate_cached_user

    from jose import jwt
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    user = await db.get(User, current_user.id, with_for_update=True, populate_existing=True)
    if user and user.active_session_id == auth_service.session_id(claims, token):
        user.active_session_id = uuid.uuid4().hex
        await auth_service.clear_session_devices(db, user)
        await db.commit()
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
    from jose import jwt
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    active_session = auth_service.session_id(claims, token)
    user = await db.get(User, current_user.id, with_for_update=True, populate_existing=True)
    if not user or user.active_session_id != active_session:
        raise HTTPException(401, "This session has ended. Sign in again.")
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
        "roles": [r.value for r in user.roles], "purpose": "access", "sid": active_session, "jti": str(uuid.uuid4())}, expires_delta=timedelta(days=settings.TENANT_SESSION_DAYS))
    from app.models.estate import Estate
    from sqlalchemy import select
    length = (await db.execute(select(Estate.token_code_length).where(Estate.app_id == user.app_id))).scalar_one_or_none() or 6
    return {"token": {"access_token": fresh, "token_type": "bearer"}, "user": user, "token_code_length": length}
