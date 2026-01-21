from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.estate import EstateCreateSchema, EstateWithSubscriptionSchema, EstateRegistrationRequestSchema, EstateRegistrationResponseSchema
from app.services import estates as estate_service

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
