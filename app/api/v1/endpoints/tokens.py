from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.token import RegistrationTokenSchema, VisitorTokenSchema, VisitorTokenCreateSchema, RegistrationTokenCreateSchema
from app.services import tokens as token_service
from app.dependencies import get_current_user, validate_tenant
from app.models.user import User, UserRole
import uuid

router = APIRouter()

@router.post("/registration", response_model=RegistrationTokenSchema)
async def create_registration_token(
    token_in: RegistrationTokenCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate a 9-character registration code for a resident.
    Only Caretakers (Estate Admins) can generate these to link residents to landlords.
    """
    if UserRole.CARETAKER not in current_user.roles:
        raise HTTPException(status_code=403, detail="Only admins can generate registration codes")
        
    return await token_service.create_registration_token(
        db, 
        landlord_id=token_in.landlord_id, 
        app_id=app_id,
        house_number=token_in.house_number,
        street_name=token_in.street_name
    )

@router.post("/registration/{code}/revoke", response_model=dict)
async def revoke_registration_token(
    code: str,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Revoke a registration code if it was exposed or is no longer valid."""
    if UserRole.CARETAKER not in current_user.roles:
        raise HTTPException(status_code=403, detail="Only admins can revoke registration codes")
        
    success = await token_service.revoke_registration_token(db, code, app_id)
    if not success:
        raise HTTPException(status_code=404, detail="Token not found")
        
    return {"status": "success", "message": f"Token {code} has been revoked"}

@router.post("/visitor", response_model=VisitorTokenSchema)
async def create_visitor_token(
    token_in: VisitorTokenCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Residents generate codes for their visitors."""
    if UserRole.RESIDENT not in current_user.roles:
         raise HTTPException(status_code=403, detail="Only residents can generate visitor codes")
         
    return await token_service.create_visitor_token(db, token_in, current_user.id, app_id)
