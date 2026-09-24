from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta, UTC
from app.models.access_log import AccessLog
from app.models.estate import Estate, Subscription
import uuid

async def get_access_logs(
    db: AsyncSession,
    app_id: str,
    limit: int = 50,
    offset: int = 0,
    event_type: Optional[str] = None,
    status: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve access logs for an estate, strictly bounded by the estate's
    plan tier retention policy.
    """
    # 1. Fetch estate subscription for retention policy
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    
    retention_days = 30
    tier_name = "starter"
    if estate and estate.subscription:
        retention_days = estate.subscription.log_retention_days
        tier_name = estate.subscription.tier

    # 2. Calculate tier retention cutoff
    cutoff_datetime = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)

    # 3. Build query bounded by retention cutoff
    query = (
        select(AccessLog)
        .where(
            AccessLog.app_id == app_id,
            AccessLog.created_at >= cutoff_datetime
        )
    )

    if event_type:
        query = query.where(AccessLog.event_type == event_type)
    if status:
        query = query.where(AccessLog.status == status)

    query = query.order_by(AccessLog.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    logs = list(result.scalars().all())

    return {
        "tier": tier_name,
        "log_retention_days": retention_days,
        "earliest_accessible_date": cutoff_datetime,
        "total_returned": len(logs),
        "logs": logs
    }

async def cleanup_expired_logs(db: AsyncSession, app_id: str) -> int:
    """
    Prune access logs that exceed the estate's plan tier retention period.
    """
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    if not estate or not estate.subscription:
        return 0

    retention_days = estate.subscription.log_retention_days
    cutoff_datetime = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)

    result = await db.execute(
        delete(AccessLog).where(
            AccessLog.app_id == app_id,
            AccessLog.created_at < cutoff_datetime
        )
    )
    await db.commit()
    return result.rowcount
