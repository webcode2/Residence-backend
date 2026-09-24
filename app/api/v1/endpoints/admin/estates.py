from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
import uuid
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.schemas.admin import AdminEstateDetailSchema, AdminEstateStatusUpdateSchema
from app.services import admin as admin_service

router = APIRouter()

@router.get("/", response_model=List[AdminEstateDetailSchema], tags=["saas_admin_estates"])
async def list_all_estates(
    search: Optional[str] = Query(None, description="Search estate by name or app_id"),
    is_active: Optional[bool] = Query(None, description="Filter by active/suspended status"),
    tier: Optional[str] = Query(None, description="Filter by subscription tier"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    List all estates across the entire SaaS platform with metrics,
    tier information, user counts, and active visitor counts.
    """
    return await admin_service.list_estates_admin(
        db=db, search=search, is_active=is_active, tier=tier
    )

@router.get("/{estate_id}", response_model=AdminEstateDetailSchema, tags=["saas_admin_estates"])
async def get_estate_details(
    estate_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """Get full details of a specific estate."""
    return await admin_service.get_estate_detail_admin(db, estate_id)

@router.patch("/{estate_id}/status", tags=["saas_admin_estates"])
async def update_estate_status(
    estate_id: uuid.UUID,
    status_in: AdminEstateStatusUpdateSchema,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    Suspend or reactivate an estate.
    Suspended estates have access immediately blocked at the gateway and API.
    """
    return await admin_service.update_estate_status_admin(db, estate_id, status_in.is_active)
