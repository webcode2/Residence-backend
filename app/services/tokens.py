from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.models.token import RegistrationToken, VisitorToken
from app.schemas.token import VisitorTokenCreateSchema
from datetime import datetime, timedelta, UTC
import secrets
import string
import uuid

def generate_registration_code() -> str:
    # 9-char alphanumeric code (e.g. REG4N8P3Q)
    # Starts with REG, followed by 6 random alphanumeric chars
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"REG{suffix}"

def generate_visitor_code() -> str:
    # 4-digit code
    return ''.join(secrets.choice(string.digits) for _ in range(4))

async def create_registration_token(db: AsyncSession, landlord_id: uuid.UUID, app_id: str, house_number: str = None, street_name: str = None) -> RegistrationToken:
    db_obj = RegistrationToken(
        code=generate_registration_code(),
        landlord_id=landlord_id,
        house_number=house_number,
        street_name=street_name,
        app_id=app_id
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj

async def revoke_registration_token(db: AsyncSession, code: str, app_id: str) -> bool:
    """Revoke a registration token so it cannot be used anymore."""
    result = await db.execute(
        update(RegistrationToken)
        .where(RegistrationToken.code == code, RegistrationToken.app_id == app_id)
        .values(is_revoked=True)
    )
    await db.commit()
    return result.rowcount > 0

async def create_visitor_token(
    db: AsyncSession, 
    token_in: VisitorTokenCreateSchema, 
    resident_id: uuid.UUID, 
    app_id: str
) -> VisitorToken:
    db_obj = VisitorToken(
        code=generate_visitor_code(),
        visitor_name=token_in.visitor_name,
        resident_id=resident_id,
        app_id=app_id,
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24) # Valid for 24h
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj
