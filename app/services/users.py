from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from app.models.user import User
from app.models.token import RegistrationToken
from app.schemas.user import UserCreateSchema, UserUpdateSchema
from app.services.auth import get_password_hash
from typing import Optional, List
import uuid

async def create_user(db: AsyncSession, user_in: UserCreateSchema) -> User:
    # Use app_id from user_in if provided
    result = await db.execute(select(User).where(User.email == user_in.email, User.app_id == user_in.app_id))
    if result.scalar_one_or_none():
        return None
    
    db_obj = User(
        email=user_in.email,
        full_name=user_in.full_name,
        phone=user_in.phone,
        hashed_password=get_password_hash(user_in.password),
        roles=user_in.roles,
        rfid_tag=user_in.rfid_tag,
        house_number=user_in.house_number,
        street_name=user_in.street_name,
        app_id=user_in.app_id,
        landlord_id=getattr(user_in, "landlord_id", None)
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj

async def get_user_by_email(db: AsyncSession, email: str, app_id: Optional[str] = None) -> Optional[User]:
    query = select(User).where(User.email == email)
    if app_id:
        query = query.where(User.app_id == app_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()

async def register_resident(db: AsyncSession, user_in: UserCreateSchema, registration_code: str, app_id: Optional[str] = None) -> Optional[User]:
    """Register a resident using a registration token."""
    from datetime import datetime, UTC
    from app.models.user import UserRole
    from app.core.redis import invalidate_cached_registration_token
    token = (await db.execute(select(RegistrationToken).where(
        RegistrationToken.code == registration_code.strip().upper(),
        *([RegistrationToken.app_id == app_id] if app_id else []),
        RegistrationToken.is_used.is_(False), RegistrationToken.is_revoked.is_(False),
        RegistrationToken.expires_at > datetime.now(UTC).replace(tzinfo=None),
    ).with_for_update())).scalar_one_or_none()
    if not token:
        return None
    app_id = token.app_id
    from app.models.estate import Estate
    estate_active = await db.scalar(select(Estate.id).where(Estate.app_id == app_id, Estate.is_active.is_(True)))
    if not estate_active:
        return None
    host = (await db.execute(select(User).where(User.id == token.landlord_id,
        User.app_id == app_id, User.is_revoked.is_(False)))).scalar_one_or_none()
    if not host or UserRole.LANDLORD not in host.roles:
        return None
    # A public invitation must never change or relink an existing account.
    if await get_user_by_email(db, str(user_in.email).lower(), app_id):
        return None
    if len(user_in.password) < 12 or len(user_in.password.encode('utf-8')) > 72:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Use at least 12 characters and no more than 72 UTF-8 bytes")
    db_obj = User(email=str(user_in.email).lower(), full_name=user_in.full_name,
        phone=user_in.phone, hashed_password=get_password_hash(user_in.password),
        roles=[UserRole.RESIDENT], house_number=token.house_number,
        street_name=token.street_name, app_id=app_id, landlord_id=token.landlord_id,
        invitation_id=token.id, relationship_type=token.relationship_type)
    db.add(db_obj)
    token.is_used = True
    await db.commit()
    await invalidate_cached_registration_token(app_id, token.code)
    await db.refresh(db_obj)
    return db_obj

async def get_user(db: AsyncSession, user_id: uuid.UUID, app_id: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.id == user_id, User.app_id == app_id))
    return result.scalar_one_or_none()

async def get_users_by_tenant(db: AsyncSession, app_id: str) -> List[User]:
    result = await db.execute(select(User).where(User.app_id == app_id))
    return list(result.scalars().all())

async def update_user(db: AsyncSession, user_id: uuid.UUID, user_in: UserUpdateSchema, app_id: str) -> Optional[User]:
    db_obj = await get_user(db, user_id, app_id)
    if not db_obj:
        return None
    
    update_data = user_in.model_dump(exclude_unset=True)
    if "password" in update_data:
        update_data["hashed_password"] = get_password_hash(update_data.pop("password"))

    old_rfid = db_obj.rfid_tag
    old_email = db_obj.email
    for field, value in update_data.items():
        setattr(db_obj, field, value)
        
    await db.commit()
    await db.refresh(db_obj)

    # Invalidate Redis user session + RFID caches
    from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user
    await invalidate_cached_user(app_id, db_obj.email)
    if old_email != db_obj.email:
        await invalidate_cached_user(app_id, old_email)
    await invalidate_cached_rfid_user(app_id, old_rfid)
    if db_obj.rfid_tag and db_obj.rfid_tag != old_rfid:
        await invalidate_cached_rfid_user(app_id, db_obj.rfid_tag)

    return db_obj

async def delete_user(db: AsyncSession, user_id: uuid.UUID, app_id: str) -> bool:
    db_obj = await get_user(db, user_id, app_id)
    if not db_obj:
        return False
    
    email = db_obj.email
    rfid_tag = db_obj.rfid_tag
    # Preserve conversation history and officer attribution when removing an account.
    # These foreign keys otherwise cascade-delete the estate's audit trail.
    from app.models.chat import ChatMessage, ChatThread
    has_threads = await db.scalar(select(ChatThread.id).where(
        ChatThread.resident_id == user_id
    ).limit(1))
    has_messages = await db.scalar(select(ChatMessage.id).where(
        ChatMessage.sender_id == user_id
    ).limit(1))
    has_household = await db.scalar(select(User.id).where(User.landlord_id == user_id).limit(1))
    has_invitations = await db.scalar(select(RegistrationToken.id).where(RegistrationToken.landlord_id == user_id).limit(1))
    from app.models.onboarding import LandlordOnboardingJob, LandlordImportFile
    has_onboarding = await db.scalar(select(LandlordOnboardingJob.id).where(LandlordOnboardingJob.user_id == user_id).limit(1))
    has_uploads = await db.scalar(select(LandlordImportFile.id).where(LandlordImportFile.uploaded_by == user_id).limit(1))
    from app.models.sos import SOSAlert
    has_sos = await db.scalar(select(SOSAlert.id).where(SOSAlert.sender_id == user_id).limit(1))
    if has_threads or has_messages or has_household or has_invitations or has_onboarding or has_uploads or has_sos:
        db_obj.is_revoked = True
        db_obj.fcm_token = None
    else:
        await db.delete(db_obj)
    await db.commit()

    from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user
    await invalidate_cached_user(app_id, email)
    await invalidate_cached_rfid_user(app_id, rfid_tag)

    return True

async def update_user_fcm_token(db: AsyncSession, user_id: uuid.UUID, fcm_token: str) -> bool:
    """Updates and persists the FCM device push notification token for the user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return False
    from app.models.sos import PushDevice
    device = await db.get(PushDevice, fcm_token)
    if device:
        device.user_id = user_id
    else:
        db.add(PushDevice(token=fcm_token, user_id=user_id))
    # A physical device must stop receiving alerts for its previous account.
    from sqlalchemy import update
    await db.execute(update(User).where(User.fcm_token==fcm_token,User.id!=user_id).values(fcm_token=None))
    user.fcm_token = fcm_token
    await db.commit()
    await db.refresh(user)
    return True
