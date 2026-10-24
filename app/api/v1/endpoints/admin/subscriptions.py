from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
import uuid
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.schemas.estate import SubscriptionBaseSchema
from app.schemas.admin import AdminSubscriptionOverrideSchema, AdminSubscriptionDetailSchema
from app.services import admin as admin_service

router = APIRouter()

@router.get("", response_model=List[AdminSubscriptionDetailSchema], tags=["saas_admin_subscriptions"])
@router.get("/", response_model=List[AdminSubscriptionDetailSchema], tags=["saas_admin_subscriptions"], include_in_schema=False)
async def list_all_subscriptions(
    status: Optional[str] = Query(None, description="Filter by status (active, expired, cancelled)"),
    tier: Optional[str] = Query(None, description="Filter by tier (starter, standard, premium, enterprise)"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """List all estate subscriptions across the platform."""
    return await admin_service.list_subscriptions_admin(db, status_filter=status, tier_filter=tier)

@router.patch("/{estate_id}", response_model=SubscriptionBaseSchema, tags=["saas_admin_subscriptions"])
async def override_estate_subscription(
    estate_id: uuid.UUID,
    override_in: AdminSubscriptionOverrideSchema,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    SaaS Admin override for an estate's subscription.
    Allows manual tier upgrade, extending expiration, adjusting verification limits,
    or toggling RFID and alert feature flags.
    """
    return await admin_service.override_subscription_admin(db, estate_id, override_in, actor=admin)


@router.get("/plans")
async def subscription_plans(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_saas_admin)):
    from app.services.plans import list_plans
    return await list_plans(db)

from app.schemas.plan import PlanUpdate
from app.core.tiers import SubscriptionTier

@router.patch("/plans/{tier}")
async def edit_subscription_plan(tier: SubscriptionTier, data: PlanUpdate,
    db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_saas_admin)):
    from app.services.plans import update_plan
    return await update_plan(db, tier.value, data, admin)
