from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.services import verification as verify_service
from app.dependencies import validate_tenant
import uuid

router = APIRouter()

@router.get("/rfid/{rfid_tag}", tags=["profiling"])
async def verify_rfid(
    rfid_tag: str,
    app_id: uuid.UUID = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db)
):
    result = await verify_service.verify_rfid_access(db, rfid_tag, app_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access Denied: Invalid tag or revoked user/landlord"
        )
    return result

@router.get("/token/{code}", tags=["profiling"])
async def verify_token(
    code: str,
    app_id: uuid.UUID = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Verify a 4-digit visitor code.
    """
    result = await verify_service.verify_visitor_token(db, code, app_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access Denied: Invalid, expired, or used token"
        )
    return result
