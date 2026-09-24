from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.schemas.admin import SaaSOverviewSchema
from app.services import admin as admin_service

router = APIRouter()

@router.get("/overview", response_model=SaaSOverviewSchema, tags=["saas_admin_analytics"])
async def get_platform_overview(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    SaaS Platform Overview:
    Returns global metrics including total estates, user role distribution,
    estimated MRR, subscription tier breakdown, and monthly verification counts.
    """
    return await admin_service.get_saas_overview(db)

@router.post("/maintenance/run", tags=["saas_admin_maintenance"])
async def trigger_platform_maintenance(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    Manually triggers the platform automated maintenance pass:
    - Purges access logs beyond tier retention windows (30/90/180/365 days).
    - Resets monthly verification quotas for 30-day billing cycles.
    - Transitions expired subscriptions to 'expired'.
    - Cleans up stale visitor tokens and expired bookout codes.
    """
    from app.services.maintenance import run_all_maintenance_jobs
    return await run_all_maintenance_jobs(db)

