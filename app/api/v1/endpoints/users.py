from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.user import UserSchema, UserCreateSchema, UserUpdateSchema
from app.schemas.estate import EstateRegistrationRequestSchema, EstateRegistrationResponseSchema
from app.services import users as user_service, estates as estate_service
from app.dependencies import validate_tenant, check_roles
from app.models.user import UserRole
import uuid

router = APIRouter()

@router.post("/register-tenant", response_model=EstateRegistrationResponseSchema, tags=["auth"])
async def create_app_admin_account(
    reg_in: EstateRegistrationRequestSchema,
    db: AsyncSession = Depends(get_db)
):
    """
    Unified registration for new Estates and their first Admin user.
    Creates Estate, Active Subscription, and Caretaker Account.
    """
    existing_user = await user_service.get_user_by_email(db, reg_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists"
        )
    
    return await estate_service.register_new_estate(db, reg_in)

@router.post("/", response_model=UserSchema, tags=["users"])
async def create_user(
    user_in: UserCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """Create a new user within the current tenant."""
    # Ensure app_id in payload matches the header app_id for safety
    user_in.app_id = app_id
    
    user = await user_service.create_user(db, user_in)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists in this tenant"
        )
    return user

# Register resident using registration code
@router.post("/register", response_model=UserSchema, tags=["users"])
async def register_resident(
    user_in: UserCreateSchema,
    registration_code: str = Query(..., description="9-char registration code"),
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db)
):
    user = await user_service.register_resident(db, user_in, registration_code, app_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid registration code or user already exists"
        )
    return user

# List users by tenant
@router.get("/", response_model=List[UserSchema], tags=["users"])
async def list_users(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    return await user_service.get_users_by_tenant(db, app_id)

@router.get("/{user_id}", response_model=UserSchema, tags=["profiling"])
async def get_user(
    user_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER, UserRole.LANDLORD, UserRole.RESIDENT]))
):
    user = await user_service.get_user(db, user_id, app_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.patch("/{user_id}", response_model=UserSchema, tags=["profiling"])
async def update_user(
    user_id: uuid.UUID,
    user_in: UserUpdateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER, UserRole.LANDLORD, UserRole.RESIDENT]))
):
    user = await user_service.update_user(db, user_id, user_in, app_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["users"])
async def delete_user(
    user_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    success = await user_service.delete_user(db, user_id, app_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return None