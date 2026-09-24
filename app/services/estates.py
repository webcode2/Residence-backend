from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.schemas.estate import EstateCreateSchema, EstateRegistrationRequestSchema
from app.core.tiers import SubscriptionTier, get_tier_config
from app.core.redis import invalidate_subscription_cache
from app.services.auth import get_password_hash
from datetime import datetime, timedelta, date, UTC
import uuid
import secrets
from fastapi import HTTPException, status

async def generate_unique_app_id(db: AsyncSession) -> str:
    """Generate a unique human-readable app_id like RP-XXXXX"""
    while True:
        app_id = f"RP-{secrets.token_hex(3).upper()}"
        result = await db.execute(select(Estate).where(Estate.app_id == app_id))
        if not result.scalar_one_or_none():
            return app_id

async def create_estate(db: AsyncSession, estate_in: EstateCreateSchema) -> Estate:
    app_id = await generate_unique_app_id(db)
    
    new_estate = Estate(
        name=estate_in.name,
        app_id=app_id,
        is_active=True
    )
    db.add(new_estate)
    await db.flush()
    
    tier_cfg = get_tier_config(estate_in.tier or SubscriptionTier.STARTER)
    new_subscription = Subscription(
        estate_id=new_estate.id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date(),
        tier=tier_cfg["tier"],
        monthly_verifications_limit=tier_cfg["monthly_verifications_limit"],
        current_month_verifications=0,
        billing_cycle_start=date.today(),
        rfid_enabled=tier_cfg["rfid_enabled"],
        log_retention_days=tier_cfg["log_retention_days"],
        realtime_alerts_enabled=tier_cfg["realtime_alerts_enabled"],
        price_monthly=tier_cfg["price_monthly"]
    )
    db.add(new_subscription)
    
    await db.commit()
    # Refresh with selectinload
    result = await db.execute(
        select(Estate)
        .where(Estate.id == new_estate.id)
        .options(selectinload(Estate.subscription))
    )
    return result.scalar_one()

async def register_new_estate(db: AsyncSession, reg_in: EstateRegistrationRequestSchema) -> dict:
    # 1. Generate unique app_id
    app_id = await generate_unique_app_id(db)
    
    # 2. Create Estate
    new_estate = Estate(
        name=reg_in.estateName,
        app_id=app_id,
        is_active=True
    )
    db.add(new_estate)
    await db.flush()
    
    # 3. Create Subscription with selected tier
    tier_cfg = get_tier_config(reg_in.tier or SubscriptionTier.STARTER)
    new_subscription = Subscription(
        estate_id=new_estate.id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date(),
        tier=tier_cfg["tier"],
        monthly_verifications_limit=tier_cfg["monthly_verifications_limit"],
        current_month_verifications=0,
        billing_cycle_start=date.today(),
        rfid_enabled=tier_cfg["rfid_enabled"],
        log_retention_days=tier_cfg["log_retention_days"],
        realtime_alerts_enabled=tier_cfg["realtime_alerts_enabled"],
        price_monthly=tier_cfg["price_monthly"]
    )
    db.add(new_subscription)
    
    # 4. Create Admin (Caretaker) User
    existing_user = await db.execute(select(User).where(User.email == reg_in.email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists"
        )
    
    admin_user = User(
        email=reg_in.email,
        full_name=reg_in.adminName,
        hashed_password=get_password_hash(reg_in.password),
        roles=[UserRole.CARETAKER],
        app_id=app_id
    )
    db.add(admin_user)
    
    await db.commit()
    
    return {
        "user": admin_user,
        "estate_name": new_estate.name,
        "app_id": app_id,
        "tier": tier_cfg["tier"]
    }

async def change_estate_tier(db: AsyncSession, app_id: str, new_tier: SubscriptionTier) -> Subscription:
    """Upgrade or downgrade an estate's subscription tier."""
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    if not estate or not estate.subscription:
        raise HTTPException(status_code=404, detail="Estate or subscription not found")

    tier_cfg = get_tier_config(new_tier)
    sub = estate.subscription
    sub.tier = tier_cfg["tier"]
    sub.monthly_verifications_limit = tier_cfg["monthly_verifications_limit"]
    sub.rfid_enabled = tier_cfg["rfid_enabled"]
    sub.log_retention_days = tier_cfg["log_retention_days"]
    sub.realtime_alerts_enabled = tier_cfg["realtime_alerts_enabled"]
    sub.price_monthly = tier_cfg["price_monthly"]

    await db.commit()
    await invalidate_subscription_cache(app_id)
    await db.refresh(sub)
    return sub

async def get_estates(db: AsyncSession) -> list[Estate]:
    result = await db.execute(select(Estate).options(selectinload(Estate.subscription)))
    return list(result.scalars().all())

async def get_estate_by_app_id(db: AsyncSession, app_id: str) -> Optional[Estate]:
    result = await db.execute(select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription)))
    return result.scalar_one_or_none()

async def get_estate_settings(db: AsyncSession, app_id: str) -> dict:
    """Fetch current estate settings including token code length and tier config."""
    estate = await get_estate_by_app_id(db, app_id)
    if not estate:
        raise HTTPException(status_code=404, detail="Estate not found")

    sub = estate.subscription
    return {
        "name": estate.name,
        "app_id": estate.app_id,
        "is_active": estate.is_active,
        "token_code_length": estate.token_code_length,
        "tier": sub.tier if sub else "starter",
        "monthly_verifications_limit": sub.monthly_verifications_limit if sub else None,
        "current_month_verifications": sub.current_month_verifications if sub else 0,
        "rfid_enabled": sub.rfid_enabled if sub else False
    }

async def update_estate_settings(db: AsyncSession, app_id: str, token_code_length: int) -> dict:
    """Update estate dashboard settings such as token code length (6 or 8)."""
    if token_code_length not in [6, 8]:
        raise HTTPException(status_code=400, detail="Token code length must be either 6 or 8 characters.")

    result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = result.scalar_one_or_none()
    if not estate:
        raise HTTPException(status_code=404, detail="Estate not found")

    estate.token_code_length = token_code_length
    await db.commit()
    await db.refresh(estate)

    # Invalidate cached subscription & tenant state in Redis
    await invalidate_subscription_cache(app_id)

    sub = estate.subscription
    return {
        "name": estate.name,
        "app_id": estate.app_id,
        "is_active": estate.is_active,
        "token_code_length": estate.token_code_length,
        "tier": sub.tier if sub else "starter",
        "monthly_verifications_limit": sub.monthly_verifications_limit if sub else None,
        "current_month_verifications": sub.current_month_verifications if sub else 0,
        "rfid_enabled": sub.rfid_enabled if sub else False
    }

