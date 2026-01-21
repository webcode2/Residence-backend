from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.user import User
from app.models.token import VisitorToken
import uuid

async def verify_rfid_access(db: AsyncSession, rfid_tag: str, app_id: str) -> dict:
    """
    Verify RFID access for a specific tenant.
    Returns a dict with 'authorized' status and user info, or raises an error in controller if None.
    """
    result = await db.execute(
        select(User).where(
            User.app_id == app_id,
            User.rfid_tag == rfid_tag,
            User.is_revoked == False
        )
    )
    user = result.scalar_one_or_none()
    
    if not user:
        return None
        
    # Extra check for resident hierarchy revocation
    from app.models.user import UserRole
    if UserRole.RESIDENT in user.roles and user.landlord_id:
        land_result = await db.execute(select(User).where(User.id == user.landlord_id))
        landlord = land_result.scalar_one_or_none()
        if not landlord or landlord.is_revoked:
             return None

    return {"status": "authorized", "user_id": str(user.id), "roles": user.roles}

async def verify_visitor_token(db: AsyncSession, code: str, app_id: str) -> dict:
    """
    Verify a 4-digit visitor code for a specific tenant.
    Ensures token is not expired and not already used.
    """
    from datetime import datetime, UTC
    
    result = await db.execute(
        select(VisitorToken).where(
            VisitorToken.code == code,
            VisitorToken.app_id == app_id,
            VisitorToken.is_used == False,
            VisitorToken.expires_at > datetime.now(UTC).replace(tzinfo=None)
        )
    )
    token = result.scalar_one_or_none()
    
    if not token:
        return None
    
    # Mark as used (1-time use logic implementation choice)
    token.is_used = True
    await db.commit()
    
    return {
        "status": "authorized", 
        "visitor_name": token.visitor_name, 
        "resident_id": str(token.resident_id)
    }
