from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.services import auth as auth_service
from app.schemas.user import TokenSchema, LoginRequestSchema, AuthResponseSchema
from app.dependencies import validate_tenant, ValidateTenant
import uuid

router = APIRouter()

@router.post("/login", response_model=AuthResponseSchema)
async def login_for_access_token(
    login_in: LoginRequestSchema,
    app_id: str = Depends(ValidateTenant(is_auth_route=True)),
    db: AsyncSession = Depends(get_db)
):
    """
    Login with JSON body.
    Requires X-App-Id header for tenant identification.
    """
    user = await auth_service.authenticate_user(
        db, email=login_in.email, password=login_in.password, app_id=app_id
    )
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if user.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is revoked",
        )

    access_token = auth_service.create_access_token(
        data={"sub": user.email, "app_id": app_id, "roles": [role.value for role in user.roles]}
    )
    return {
        "token": {"access_token": access_token, "token_type": "bearer"},
        "user": user
    }
