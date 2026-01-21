from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.billing import BillingSchema, BillingCreateSchema
from app.services import billings as billing_service
from app.dependencies import validate_tenant
import uuid

router = APIRouter()

@router.post("/", response_model=BillingSchema)
async def create_billing(
    billing_in: BillingCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db)
):
    # Ensure app_id matches
    billing_in.app_id = app_id
    return await billing_service.create_billing(db, billing_in)

@router.get("/", response_model=List[BillingSchema])
async def list_billings(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db)
):
    return await billing_service.get_billings_by_app(db, app_id)

@router.get("/user/{user_id}", response_model=List[BillingSchema])
async def get_user_billings(
    user_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db)
):
    return await billing_service.get_billings_by_user(db, user_id, app_id)
