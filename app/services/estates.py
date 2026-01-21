from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.schemas.estate import EstateCreateSchema, EstateRegistrationRequestSchema
from app.services.auth import get_password_hash
from datetime import datetime, timedelta, UTC
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
    app_id =  await generate_unique_app_id(db)
    
    new_estate = Estate(
        name=estate_in.name,
        app_id=app_id,
        is_active=True
    )
    db.add(new_estate)
    await db.flush()
    
    new_subscription = Subscription(
        estate_id=new_estate.id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date()
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
    
    # 3. Create Active Subscription (1 year)
    new_subscription = Subscription(
        estate_id=new_estate.id,
        status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date()
    )
    db.add(new_subscription)
    
    # 4. Create Admin (Caretaker) User
    # verify email duplicate
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
    
    # Return both User and Estate details as usually expected by frontend
    return {
        "user": admin_user,
        "estate_name": new_estate.name,
        "app_id": app_id
    }

async def get_estates(db: AsyncSession) -> list[Estate]:
    result = await db.execute(select(Estate).options(selectinload(Estate.subscription)))
    return list(result.scalars().all())

async def get_estate_by_app_id(db: AsyncSession, app_id: str) -> Optional[Estate]:
    result = await db.execute(select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription)))
    return result.scalar_one_or_none()
