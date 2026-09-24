from fastapi import Header, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, UserRole
from app.models.estate import Estate
from app.schemas.user import TokenDataSchema
from typing import List
import uuid

# OAuth2 scheme for token extraction
from app.core.redis import (
    get_cached_estate_validity,
    set_cached_estate_validity,
    get_cached_user,
    set_cached_user,
    is_token_blacklisted,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")

class ValidateTenant:
    def __init__(self, is_auth_route: bool = False):
        self.is_auth_route = is_auth_route

    async def __call__(self, x_app_id: str = Header(...), db: AsyncSession = Depends(get_db)) -> str:
        """
        Validate that the app_id exists.
        Expected in 'X-App-Id' header.
        Fast-path: Check Redis cache first to avoid hitting PostgreSQL on every API call.
        """
        cached_valid = await get_cached_estate_validity(x_app_id)
        if cached_valid is True:
            return x_app_id
        if cached_valid is False:
            if self.is_auth_route:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect login credentials"
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid application id"
            )

        # Cache miss -> Query DB
        result = await db.execute(select(Estate).where(Estate.app_id == x_app_id))
        estate = result.scalar_one_or_none()
       
        if not estate:
            await set_cached_estate_validity(x_app_id, is_valid=False, ttl_seconds=300)
            if self.is_auth_route:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect login credentials"
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid application id"
            )
            
        await set_cached_estate_validity(x_app_id, is_valid=True, ttl_seconds=86400)
        return x_app_id

# Default instance for most routes
validate_tenant = ValidateTenant()




async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    app_id: str = Depends(validate_tenant)
) -> User:
    """
    Authenticate and load current user.
    Fast-path:
    1. Validates JWT signature and expiration.
    2. Checks Redis token blacklist (O(1)).
    3. Fetches cached user profile from Redis (O(1), < 1ms).
    4. Falls back to PostgreSQL on cache miss, then caches in Redis for 30 minutes.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email: str = payload.get("sub")
        token_app_id: str = payload.get("app_id")
        if email is None or token_app_id != app_id:
            raise credentials_exception
        token_data = TokenDataSchema(email=email, app_id=token_app_id)
    except JWTError:
        raise credentials_exception

    # 1. Fast-path: Check Redis token blacklist (e.g., after logout or revocation)
    if await is_token_blacklisted(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked or logged out",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Fast-path: Check Redis user profile cache
    cached_user = await get_cached_user(token_data.app_id, token_data.email)
    if cached_user:
        if cached_user.get("is_revoked", False):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return User(
            id=uuid.UUID(cached_user["id"]),
            email=cached_user["email"],
            full_name=cached_user.get("full_name"),
            hashed_password=cached_user.get("hashed_password", ""),
            roles=[UserRole(r) for r in cached_user.get("roles", [])],
            rfid_tag=cached_user.get("rfid_tag"),
            house_number=cached_user.get("house_number"),
            street_name=cached_user.get("street_name"),
            is_revoked=cached_user.get("is_revoked", False),
            app_id=cached_user["app_id"],
            landlord_id=uuid.UUID(cached_user["landlord_id"]) if cached_user.get("landlord_id") else None
        )

    # 3. Database Fallback on Cache Miss
    result = await db.execute(
        select(User).where(User.email == token_data.email, User.app_id == token_data.app_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
        
    if user.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4. Cache user in Redis for subsequent requests (30-minute TTL)
    await set_cached_user(
        app_id=token_data.app_id,
        email=token_data.email,
        user_data={
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "hashed_password": user.hashed_password,
            "roles": [r.value for r in user.roles],
            "rfid_tag": user.rfid_tag,
            "house_number": user.house_number,
            "street_name": user.street_name,
            "is_revoked": user.is_revoked,
            "app_id": user.app_id,
            "landlord_id": str(user.landlord_id) if user.landlord_id else None
        },
        ttl_seconds=1800
    )

    return user

def check_roles(allowed_roles: List[UserRole]):
    async def role_checker(current_user: User = Depends(get_current_user)):
        # Check if user has at least one of the allowed roles
        user_role_set = set(current_user.roles)
        allowed_role_set = set(allowed_roles)
        if not user_role_set.intersection(allowed_role_set):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have enough permissions to access this resource"
            )
        return current_user
    return role_checker

async def check_user_access(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> User:
    """Validate if current user can access data for given user_id."""
    # SaaS Admin (Caretaker) can access everyone in their tenant
    if UserRole.CARETAKER in current_user.roles:
        target_user_result = await db.execute(
            select(User).where(User.id == user_id, User.app_id == current_user.app_id)
        )
        target_user = target_user_result.scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        return target_user

    # Landlord can access their residents
    if UserRole.LANDLORD in current_user.roles:
        target_user_result = await db.execute(
            select(User).where(User.id == user_id, User.landlord_id == current_user.id)
        )
        target_user = target_user_result.scalar_one_or_none()
        if not target_user:
             raise HTTPException(status_code=403, detail="Access denied")
        return target_user

    # Resident can only access themselves
    if current_user.id == user_id:
        return current_user
        
    raise HTTPException(status_code=403, detail="Access denied")

async def get_current_saas_admin(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Dependency that authenticates a SaaS Admin (Platform Owner).
    Does NOT require the 'X-App-Id' header since SaaS admins operate globally across all tenants.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    forbidden_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="SaaS Admin (saas_owner) privileges required to access this resource",
    )

    try:
        payload = jwt.decode(token, settings.SAAS_ADMIN_SECRET_KEY, algorithms=[settings.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Check token blacklist in Redis
    if await is_token_blacklisted(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked or logged out",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fetch user from DB
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception

    if user.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is revoked",
        )

    if UserRole.SAAS_OWNER not in user.roles:
        raise forbidden_exception

    return user

