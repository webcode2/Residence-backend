from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.models.user import User, UserRole
from app.schemas.user import UserCreateSchema
from app.schemas.landlord import LandlordImportSchema, LandlordImportResponseSchema
from app.core.config import settings
from app.services import users as user_service
from app.services.notifications import notification_service
import secrets
import string

async def import_landlords(
    db: AsyncSession, 
    import_data: List[LandlordImportSchema],
    app_id: str
) -> List[LandlordImportResponseSchema]:
    results = []
    
    for entry in import_data:
        # Check if user already exists
        existing_user = await user_service.get_user_by_email(db, entry.email)
        if existing_user:
            results.append(LandlordImportResponseSchema(
                email=entry.email,
                status="skipped",
                message="User already exists"
            ))
            continue
            
        # Generate temporary password
        temp_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        
        # Create Landlord account
        landlord_in = UserCreateSchema(
            email=entry.email,
            full_name=entry.name or "Landlord",
            password=temp_password,
            roles=[UserRole.LANDLORD],
            house_number=entry.house_number,
            street_name=entry.street_name,
            app_id=app_id
        )
        
        new_user = await user_service.create_user(db, landlord_in)
        
        if new_user:
            # Send notification email (asynchronously)
            # In a production app, this would be a background task (e.g. Celery)
            email_sent = await notification_service.send_registration_email(
                email=entry.email,
                password=temp_password,
                full_name=landlord_in.full_name
            )
            
            results.append(LandlordImportResponseSchema(
                email=entry.email,
                status="success",
                message="Account created and email sent" if email_sent else "Account created but email failed"
            ))
        else:
            results.append(LandlordImportResponseSchema(
                email=entry.email,
                status="error",
                message="Failed to create account"
            ))
            
    return results
