from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.estate import (
    EstateCreateSchema, 
    EstateWithSubscriptionSchema, 
    EstateRegistrationRequestSchema, 
    EstateRegistrationResponseSchema,
    EstateSettingsResponseSchema,
    EstateSettingsUpdateSchema
)
from app.services import estates as estate_service
from app.dependencies import validate_tenant, check_roles
from app.models.user import User, UserRole

router = APIRouter()

@router.post("/", response_model=EstateWithSubscriptionSchema)
async def create_estate(
    estate_in: EstateCreateSchema,
    db: AsyncSession = Depends(get_db)
):
    """Internal/Super Admin creation."""
    return await estate_service.create_estate(db, estate_in)

@router.post("/register", response_model=EstateRegistrationResponseSchema)
async def register_estate(
    reg_in: EstateRegistrationRequestSchema,
    db: AsyncSession = Depends(get_db)
):
    """Unified registration for new Estates and their first Admin user."""
    return await estate_service.register_new_estate(db, reg_in)

@router.get("/", response_model=list[EstateWithSubscriptionSchema])
async def list_estates(db: AsyncSession = Depends(get_db)):
    return await estate_service.get_estates(db)

@router.get("/settings", response_model=EstateSettingsResponseSchema, tags=["estates"])
async def get_estate_settings(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """Estate admin views estate dashboard settings including token code length."""
    return await estate_service.get_estate_settings(db, app_id)

@router.patch("/settings", response_model=EstateSettingsResponseSchema, tags=["estates"])
async def update_estate_settings(
    settings_in: EstateSettingsUpdateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """Estate admin updates estate dashboard settings (e.g. token code length to 6 or 8)."""
    return await estate_service.update_estate_settings(db, app_id, settings_in.token_code_length)

