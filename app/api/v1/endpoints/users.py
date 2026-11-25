from fastapi import APIRouter, Depends, HTTPException, status, Query, Header
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from app.core.database import get_db
from app.schemas.user import UserSchema, UserCreateSchema, ResidentRegistrationSchema, SelfProfileUpdateSchema, AuthResponseSchema, UserUpdateSchema, FCMTokenUpdateSchema
from app.schemas.estate import EstateRegistrationRequestSchema, EstateRegistrationResponseSchema
from app.services import users as user_service, estates as estate_service
from app.dependencies import validate_tenant, check_roles, get_current_user, check_user_access, oauth2_scheme
from app.models.user import User, UserRole
import uuid

router = APIRouter()

@router.post("/me/fcm-token", response_model=dict, tags=["users"])
async def update_my_fcm_token(
    fcm_in: FCMTokenUpdateSchema,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update or register the FCM device push notification token for the logged-in user.
    Called automatically on app launch, login, or when FCM rotates the device token.
    """
    success = await user_service.update_user_fcm_token(db, current_user.id, fcm_in.fcm_token)
    if not success:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "success", "message": "FCM token synced successfully"}

@router.get("/me/profile", response_model=UserSchema, tags=["profiling"])
async def get_my_profile(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await db.get(User, current_user.id, populate_existing=True)

@router.patch("/me/profile", response_model=AuthResponseSchema, tags=["profiling"])
async def update_my_profile(
    profile: SelfProfileUpdateSchema,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.profiles import update_profile
    return await update_profile(db, current_user, profile, token)

@router.post("/register-tenant", response_model=EstateRegistrationResponseSchema, tags=["auth"])
async def create_app_admin_account(
    reg_in: EstateRegistrationRequestSchema,
    db: AsyncSession = Depends(get_db)
):
    """
    Unified registration for new Estates and their first Admin user.
    Creates Estate, Active Subscription, and Caretaker Account.
    """
    # Accounts are unique per estate; a brand-new estate has none, so the same person may run several estates.
    return await estate_service.register_new_estate(db, reg_in)

@router.post("/", response_model=UserSchema, tags=["users"])
async def create_user(
    user_in: UserCreateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """Create a new user within the current tenant."""
    if UserRole.SAAS_OWNER in user_in.roles:
        raise HTTPException(status_code=403, detail="Platform owner roles cannot be assigned by estate accounts")
    if UserRole.LANDLORD in user_in.roles:
        raise HTTPException(status_code=409, detail="Hosts / Property Owners must be onboarded through the caretaker import queue")
    # Ensure app_id in payload matches the header app_id for safety
    user_in.app_id = app_id
    
    user = await user_service.create_user(db, user_in)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists in this tenant"
        )
    
    # Create active registration token alongside user creation
    if UserRole.LANDLORD in user.roles or UserRole.RESIDENT in user.roles:
        from app.models.token import RegistrationToken
        from app.services.tokens import generate_registration_code
        from app.core.redis import cache_registration_token
        from datetime import datetime, timedelta, UTC

        token = RegistrationToken(
            code=generate_registration_code(),
            landlord_id=user.landlord_id or user.id,
            house_number=user.house_number,
            street_name=user.street_name,
            relationship_type="family",
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7),
            app_id=app_id
        )
        db.add(token)
        await db.commit()
        try:
            await cache_registration_token(
                app_id=app_id,
                code=token.code,
                token_data={
                    "id": str(token.id),
                    "code": token.code,
                    "landlord_id": str(token.landlord_id),
                    "app_id": app_id,
                    "house_number": token.house_number,
                    "street_name": token.street_name
                },
                ttl_seconds=1200
            )
        except Exception:
            pass

    return user

# Register resident using registration code
@router.post("/register", response_model=UserSchema, tags=["users"])
async def register_resident(
    user_in: ResidentRegistrationSchema,
    registration_code: str = Query(..., description="9-char registration code"),
    x_app_id: Optional[str] = Header(None, alias="X-App-Id"),
    db: AsyncSession = Depends(get_db)
):
    user = await user_service.register_resident(db, user_in, registration_code, x_app_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid registration code or user already exists"
        )
    return user

# List users by tenant
@router.get("/", response_model=List[UserSchema], tags=["users"])
async def list_users(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    return await user_service.get_users_by_tenant(db, app_id)

@router.get("/{user_id}", response_model=UserSchema, tags=["profiling"])
async def get_user(
    user_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    await check_user_access(user_id, db, current_user)
    user = await user_service.get_user(db, user_id, app_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.patch("/{user_id}", response_model=UserSchema, tags=["profiling"])
async def update_user(
    user_id: uuid.UUID,
    user_in: UserUpdateSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if UserRole.CARETAKER not in current_user.roles:
        if current_user.id != user_id:
            raise HTTPException(status_code=403, detail="You can only update your own profile")
        if user_in.model_fields_set - {"full_name", "password"}:
            raise HTTPException(status_code=403, detail="Only caretakers can change estate account permissions and identity")
    if user_in.roles is not None and UserRole.SAAS_OWNER in user_in.roles:
        raise HTTPException(status_code=403, detail="Platform owner roles cannot be assigned by estate accounts")
    if user_in.roles is not None and UserRole.LANDLORD in user_in.roles:
        target = await user_service.get_user(db, user_id, app_id)
        if target and UserRole.LANDLORD not in target.roles:
            raise HTTPException(status_code=409, detail="Hosts / Property Owners must be onboarded through the caretaker import queue")
    user = await user_service.update_user(db, user_id, user_in, app_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.delete("/{user_id}", tags=["users"])
async def delete_user(
    user_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    result = await user_service.delete_user(db, user_id, app_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    if result == "revoked":
        return {
            "status": "revoked",
            "message": "Account has related records and was suspended to preserve history. Data was not permanently deleted.",
        }
    return {"status": "deleted", "message": "Account permanently deleted."}


@router.patch("/{user_id}/status", response_model=UserSchema, tags=["users"])
async def set_user_status(
    user_id: uuid.UUID,
    body: dict,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """Suspend or restore a user account within the estate."""
    if "is_revoked" not in body or not isinstance(body["is_revoked"], bool):
        raise HTTPException(status_code=422, detail="is_revoked boolean is required")
    target = await user_service.get_user(db, user_id, app_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if UserRole.SAAS_OWNER in target.roles:
        raise HTTPException(status_code=403, detail="Platform owner accounts cannot be managed here")
    if target.id == current_user.id:
        raise HTTPException(status_code=409, detail="You cannot change your own account status")
    target.is_revoked = body["is_revoked"]
    if body["is_revoked"]:
        target.fcm_token = None
    await db.commit()
    await db.refresh(target)
    from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user
    await invalidate_cached_user(app_id, target.email)
    await invalidate_cached_rfid_user(app_id, target.rfid_tag)
    if UserRole.LANDLORD in target.roles:
        from sqlalchemy import select
        residents = (await db.execute(select(User).where(User.landlord_id == target.id, User.app_id == app_id))).scalars().all()
        for resident in residents:
            await invalidate_cached_rfid_user(app_id, resident.rfid_tag)
            await invalidate_cached_user(app_id, resident.email)
    return target
@router.delete("/me/fcm-token", tags=["users"])
async def unregister_my_device(fcm_in: FCMTokenUpdateSchema, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from sqlalchemy import delete, select
    from app.models.sos import PushDevice
    await db.execute(delete(PushDevice).where(PushDevice.token==fcm_in.fcm_token,PushDevice.user_id==current_user.id))
    user = await db.get(User,current_user.id)
    if user and user.fcm_token==fcm_in.fcm_token: user.fcm_token=None
    await db.commit()
    return {"status":"success"}
