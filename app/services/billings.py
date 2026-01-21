from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.billings import Billing
from app.schemas.billing import BillingCreateSchema
from typing import List
import uuid

async def create_billing(db: AsyncSession, billing_in: BillingCreateSchema) -> Billing:
    db_obj = Billing(
        user_id=billing_in.user_id,
        amount=billing_in.amount,
        title=billing_in.title,
        description=billing_in.description,
        app_id=billing_in.app_id
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj

async def get_billings_by_app(db: AsyncSession, app_id: str) -> List[Billing]:
    result = await db.execute(select(Billing).where(Billing.app_id == app_id))
    return list(result.scalars().all())

async def get_billings_by_user(db: AsyncSession, user_id: uuid.UUID, app_id: str) -> List[Billing]:
    result = await db.execute(
        select(Billing).where(Billing.user_id == user_id, Billing.app_id == app_id)
    )
    return list(result.scalars().all())
