from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.landlord import LandlordImportSchema, LandlordImportResponseSchema
from app.services import landlords as landlord_service
from app.dependencies import check_roles, validate_tenant
from app.models.user import UserRole
import uuid

router = APIRouter()

@router.post("/import", response_model=List[LandlordImportResponseSchema])
async def import_landlords(
    import_data: List[LandlordImportSchema],
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Bulk import landlords.
    Only accessible by Caretakers (Estate Admins).
    """
    return await landlord_service.import_landlords(db, import_data, app_id)
