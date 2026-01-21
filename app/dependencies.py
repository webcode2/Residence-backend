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
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")

class ValidateTenant:
    def __init__(self, is_auth_route: bool = False):
        self.is_auth_route = is_auth_route

    async def __call__(self, x_app_id: str = Header(...), db: AsyncSession = Depends(get_db)) -> str:
        """
        Validate that the app_id exists.
        Expected in 'X-App-Id' header.
        """
        result = await db.execute(select(Estate).where(Estate.app_id == x_app_id))
        estate = result.scalar_one_or_none()
       
        if not estate:
            if self.is_auth_route:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect login credentials"
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid application id"
            )
        return x_app_id

# Default instance for most routes
validate_tenant = ValidateTenant()




async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    app_id: str = Depends(validate_tenant)
) -> User:
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
        
    result = await db.execute(
        select(User).where(User.email == token_data.email, User.app_id == token_data.app_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
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
