from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, or_
from sqlalchemy.orm import selectinload
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import VisitorToken
from app.models.access_log import AccessLog
from app.schemas.admin import (
    SaaSOverviewSchema,
    AdminEstateDetailSchema,
    AdminSubscriptionOverrideSchema,
    AdminUserDetailSchema
)
from app.core.tiers import SubscriptionTier, get_tier_config
from app.core.redis import invalidate_subscription_cache, invalidate_cached_user, set_cached_estate_validity, invalidate_cached_rfid_user
from datetime import datetime, UTC
from fastapi import HTTPException, status
import uuid

async def get_saas_overview(db: AsyncSession) -> dict:
    """Calculates platform-wide KPIs for the SaaS Admin dashboard."""
    # 1. Estate counts
    total_estates_res = await db.execute(select(func.count(Estate.id)))
    total_estates = total_estates_res.scalar() or 0

    active_estates_res = await db.execute(select(func.count(Estate.id)).where(Estate.is_active == True))
    active_estates = active_estates_res.scalar() or 0
    suspended_estates = total_estates - active_estates

    # 2. Total Users and Role Breakdown
    total_users_res = await db.execute(select(func.count(User.id)))
    total_users = total_users_res.scalar() or 0

    users_by_role = {role.value: 0 for role in UserRole}
    all_users = (await db.execute(select(User.roles))).scalars().all()
    for roles_list in all_users:
        if roles_list:
            for r in roles_list:
                role_val = r.value if hasattr(r, "value") else str(r)
                users_by_role[role_val] = users_by_role.get(role_val, 0) + 1

    # 3. Subscriptions & Estimated MRR
    subs_res = await db.execute(select(Subscription))
    all_subs = subs_res.scalars().all()
    subscriptions_by_tier = {tier.value: 0 for tier in SubscriptionTier}
    estimated_mrr = 0.0
    total_verifications_this_month = 0

    for sub in all_subs:
        if sub.status == "active":
            subscriptions_by_tier[sub.tier] = subscriptions_by_tier.get(sub.tier, 0) + 1
            estimated_mrr += float(sub.price_monthly or 0.0)
        total_verifications_this_month += sub.current_month_verifications

    # 4. Currently on-premises visitors across all estates
    active_visitors_res = await db.execute(
        select(func.count(VisitorToken.id)).where(VisitorToken.status == "checked_in")
    )
    total_active_visitors = active_visitors_res.scalar() or 0

    return {
        "total_estates": total_estates,
        "active_estates": active_estates,
        "suspended_estates": suspended_estates,
        "total_users": total_users,
        "users_by_role": users_by_role,
        "subscriptions_by_tier": subscriptions_by_tier,
        "estimated_mrr": round(estimated_mrr, 2),
        "total_verifications_this_month": total_verifications_this_month,
        "total_active_visitors": total_active_visitors,
    }

async def list_estates_admin(
    db: AsyncSession,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    tier: Optional[str] = None
) -> List[dict]:
    """Lists all estates with subscription details and usage counts for SaaS admin."""
    query = select(Estate).options(selectinload(Estate.subscription)).order_by(Estate.created_at.desc())

    if search:
        query = query.where(
            or_(
                Estate.name.ilike(f"%{search}%"),
                Estate.app_id.ilike(f"%{search}%")
            )
        )
    if is_active is not None:
        query = query.where(Estate.is_active == is_active)

    estates = (await db.execute(query)).scalars().all()
    results = []

    for estate in estates:
        sub = estate.subscription
        if tier and (not sub or sub.tier != tier):
            continue

        # Count users in estate
        u_count = (await db.execute(select(func.count(User.id)).where(User.app_id == estate.app_id))).scalar() or 0
        v_active = (await db.execute(
            select(func.count(VisitorToken.id)).where(
                VisitorToken.app_id == estate.app_id,
                VisitorToken.status == "checked_in"
            )
        )).scalar() or 0

        results.append({
            "id": estate.id,
            "name": estate.name,
            "app_id": estate.app_id,
            "is_active": estate.is_active,
            "token_code_length": estate.token_code_length,
            "created_at": estate.created_at,
            "subscription": sub,
            "user_count": u_count,
            "active_visitors_count": v_active,
            "total_verifications": sub.current_month_verifications if sub else 0
        })

    return results

async def get_estate_detail_admin(db: AsyncSession, estate_id: uuid.UUID) -> dict:
    result = await db.execute(
        select(Estate).where(Estate.id == estate_id).options(selectinload(Estate.subscription))
    )
    estate = result.scalar_one_or_none()
    if not estate:
        raise HTTPException(status_code=404, detail="Estate not found")

    sub = estate.subscription
    u_count = (await db.execute(select(func.count(User.id)).where(User.app_id == estate.app_id))).scalar() or 0
    v_active = (await db.execute(
        select(func.count(VisitorToken.id)).where(
            VisitorToken.app_id == estate.app_id,
            VisitorToken.status == "checked_in"
        )
    )).scalar() or 0

    return {
        "id": estate.id,
        "name": estate.name,
        "app_id": estate.app_id,
        "is_active": estate.is_active,
        "token_code_length": estate.token_code_length,
        "created_at": estate.created_at,
        "subscription": sub,
        "user_count": u_count,
        "active_visitors_count": v_active,
        "total_verifications": sub.current_month_verifications if sub else 0
    }

async def update_estate_status_admin(db: AsyncSession, estate_id: uuid.UUID, is_active: bool) -> dict:
    result = await db.execute(select(Estate).where(Estate.id == estate_id))
    estate = result.scalar_one_or_none()
    if not estate:
        raise HTTPException(status_code=404, detail="Estate not found")

    estate.is_active = is_active
    await db.commit()
    await db.refresh(estate)

    # Sync Redis tenant cache
    await set_cached_estate_validity(estate.app_id, is_valid=is_active)
    await invalidate_subscription_cache(estate.app_id)

    return {"id": estate.id, "app_id": estate.app_id, "is_active": estate.is_active, "message": f"Estate status set to {'active' if is_active else 'suspended'}"}

async def list_subscriptions_admin(
    db: AsyncSession,
    status_filter: Optional[str] = None,
    tier_filter: Optional[str] = None
) -> List[Subscription]:
    query = select(Subscription).options(selectinload(Subscription.estate)).order_by(Subscription.expiry_date.asc())
    if status_filter:
        query = query.where(Subscription.status == status_filter)
    if tier_filter:
        query = query.where(Subscription.tier == tier_filter)

    results = await db.execute(query)
    return list(results.scalars().all())

async def override_subscription_admin(
    db: AsyncSession,
    estate_id: uuid.UUID,
    override_in: AdminSubscriptionOverrideSchema
) -> Subscription:
    result = await db.execute(
        select(Subscription).where(Subscription.estate_id == estate_id).options(selectinload(Subscription.estate))
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found for this estate")

    if override_in.tier is not None:
        tier_cfg = get_tier_config(override_in.tier)
        sub.tier = tier_cfg["tier"]
        sub.monthly_verifications_limit = tier_cfg["monthly_verifications_limit"]
        sub.rfid_enabled = tier_cfg["rfid_enabled"]
        sub.log_retention_days = tier_cfg["log_retention_days"]
        sub.realtime_alerts_enabled = tier_cfg["realtime_alerts_enabled"]
        sub.price_monthly = tier_cfg["price_monthly"]

    if override_in.status is not None:
        sub.status = override_in.status
    if override_in.expiry_date is not None:
        sub.expiry_date = override_in.expiry_date
    if override_in.monthly_verifications_limit is not None:
        sub.monthly_verifications_limit = override_in.monthly_verifications_limit
    if override_in.rfid_enabled is not None:
        sub.rfid_enabled = override_in.rfid_enabled
    if override_in.realtime_alerts_enabled is not None:
        sub.realtime_alerts_enabled = override_in.realtime_alerts_enabled
    if override_in.log_retention_days is not None:
        sub.log_retention_days = override_in.log_retention_days

    await db.commit()
    await db.refresh(sub)

    if sub.estate:
        await invalidate_subscription_cache(sub.estate.app_id)

    return sub

async def search_users_admin(
    db: AsyncSession,
    query_str: Optional[str] = None,
    role_filter: Optional[str] = None,
    limit: int = 50
) -> List[dict]:
    """Search users across all tenants."""
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)

    if query_str:
        stmt = stmt.where(
            or_(
                User.email.ilike(f"%{query_str}%"),
                User.full_name.ilike(f"%{query_str}%"),
                User.rfid_tag.ilike(f"%{query_str}%"),
                User.app_id.ilike(f"%{query_str}%")
            )
        )

    users = (await db.execute(stmt)).scalars().all()
    results = []
    
    # Pre-fetch estates for names
    estate_names = {}
    estates_res = await db.execute(select(Estate.app_id, Estate.name))
    for row in estates_res.all():
        estate_names[row[0]] = row[1]

    for u in users:
        role_vals = [r.value if hasattr(r, "value") else str(r) for r in u.roles]
        if role_filter and role_filter not in role_vals:
            continue

        results.append({
            "id": u.id,
            "email": u.email,
            "full_name": u.full_name,
            "roles": role_vals,
            "app_id": u.app_id,
            "estate_name": estate_names.get(u.app_id, "Unknown"),
            "rfid_tag": u.rfid_tag,
            "house_number": u.house_number,
            "street_name": u.street_name,
            "is_revoked": u.is_revoked,
            "created_at": u.created_at
        })

    return results

async def set_user_revocation_admin(db: AsyncSession, user_id: uuid.UUID, is_revoked: bool) -> dict:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_revoked = is_revoked
    await db.commit()
    await db.refresh(user)

    # Invalidate Redis caches for this user
    await invalidate_cached_user(user.app_id, user.email)
    await invalidate_cached_rfid_user(user.app_id, user.rfid_tag)

    # Landlord revocation must invalidate resident RFID caches immediately
    if UserRole.LANDLORD in user.roles:
        residents = await db.execute(select(User).where(User.landlord_id == user.id))
        for resident in residents.scalars().all():
            await invalidate_cached_rfid_user(user.app_id, resident.rfid_tag)
            await invalidate_cached_user(user.app_id, resident.email)

    return {
        "id": user.id,
        "email": user.email,
        "is_revoked": user.is_revoked,
        "message": f"User account has been {'revoked' if is_revoked else 'reinstated'}"
    }

async def get_global_access_logs(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    status_filter: Optional[str] = None
) -> List[AccessLog]:
    """Query audit logs across all estates."""
    query = select(AccessLog).order_by(AccessLog.timestamp.desc()).offset(offset).limit(limit)
    if status_filter:
        query = query.where(AccessLog.status == status_filter)

    results = await db.execute(query)
    return list(results.scalars().all())
