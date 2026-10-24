from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.estate import (
    EstateCreateSchema, 
    EstateWithSubscriptionSchema, 
    EstateRegistrationRequestSchema, 
    EstateRegistrationResponseSchema,
    EstateSettingsResponseSchema,
    EstateSettingsUpdateSchema
)
from app.services import estates as estate_service
from app.dependencies import validate_tenant, check_roles, get_current_saas_admin, get_current_user
from app.models.user import User, UserRole

router = APIRouter()

@router.post("/", response_model=EstateWithSubscriptionSchema)
async def create_estate(
    estate_in: EstateCreateSchema,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """Internal/Super Admin creation."""
    return await estate_service.create_estate(db, estate_in)

@router.post("/register", response_model=EstateRegistrationResponseSchema)
async def register_estate(
    reg_in: EstateRegistrationRequestSchema,
    db: AsyncSession = Depends(get_db)
):
    """Unified registration for new Estates and their first Admin user."""
    return await estate_service.register_new_estate(db, reg_in)

@router.get("/", response_model=list[EstateWithSubscriptionSchema])
async def list_estates(db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_saas_admin)):
    return await estate_service.get_estates(db)

@router.get("/settings", response_model=EstateSettingsResponseSchema, tags=["estates"])
async def get_estate_settings(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """Estate admin views estate dashboard settings including token code length."""
    return await estate_service.get_estate_settings(db, app_id)

@router.patch("/settings", response_model=EstateSettingsResponseSchema, tags=["estates"])
async def update_estate_settings(
    settings_in: EstateSettingsUpdateSchema,
    background_tasks: BackgroundTasks,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """Estate admin updates estate dashboard settings (e.g. token code length to 6 or 8)."""
    from app.services import estate_settings_sync
    result = await estate_service.update_estate_settings(db, app_id, settings_in.token_code_length)
    tokens = await estate_settings_sync.recipients(db, app_id)
    background_tasks.add_task(estate_settings_sync.notify, tokens, app_id)
    return result

@router.get("/security-tips", response_model=list[dict], tags=["estates"])
async def get_security_tips():
    """
    Returns curated security tips for residents and estate gates.
    Displayed on the resident mobile app dashboard.
    """
    return [
        {
            "id": 1,
            "category": "Pass Safety",
            "tip": "Only generate visitor passes for people you know and expect.",
            "icon": "shield"
        },
        {
            "id": 2,
            "category": "Exit Verification",
            "tip": "Generate the 4-digit exit code only when your visitor is departing.",
            "icon": "logout"
        },
        {
            "id": 3,
            "category": "Anti-Fraud",
            "tip": "Security guards will never ask for your personal app login password.",
            "icon": "lock"
        },
        {
            "id": 4,
            "category": "Gate Etiquette",
            "tip": "Tailgating at the boom barrier is forbidden. Let each car scan separately.",
            "icon": "car"
        },
        {
            "id": 5,
            "category": "Biometrics",
            "tip": "Enable Biometric App Lock in Settings for faster, secure OTP access.",
            "icon": "fingerprint"
        }
    ]




@router.get('/mobile-settings', tags=['estates'])
async def get_mobile_settings(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.services import estate_settings_sync
    return await estate_settings_sync.snapshot(db, app_id)
