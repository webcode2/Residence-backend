from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, exists
from app.models.user import User
from app.models.token import RegistrationToken
from app.schemas.user import UserCreateSchema, UserUpdateSchema
from app.services.auth import get_password_hash_async
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
        hashed_password=await get_password_hash_async(user_in.password),
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
    from app.core.redis import invalidate_cached_user
    await invalidate_cached_user(db_obj.app_id, db_obj.email)
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
    from app.models.estate import Estate
    from app.core.redis import invalidate_cached_registration_token
    email = str(user_in.email).lower()
    # One round trip instead of four: lock the invitation and, in the same statement,
    # check the estate is active, the host is an active landlord of that estate and
    # the email is not already registered there (all correlated to the token row).
    estate_active = select(Estate.id).where(
        Estate.app_id == RegistrationToken.app_id, Estate.is_active.is_(True)).exists()
    host_roles = select(User.roles).where(
        User.id == RegistrationToken.landlord_id, User.app_id == RegistrationToken.app_id,
        User.is_revoked.is_(False)).scalar_subquery()
    email_taken = select(User.id).where(
        User.email == email, User.app_id == RegistrationToken.app_id).exists()
    row = (await db.execute(select(
        RegistrationToken,
        estate_active.label("estate_active"),
        host_roles.label("host_roles"),
        email_taken.label("email_taken"),
    ).where(
        RegistrationToken.code == registration_code.strip().upper(),
        *([RegistrationToken.app_id == app_id] if app_id else []),
        RegistrationToken.is_used.is_(False), RegistrationToken.is_revoked.is_(False),
        RegistrationToken.expires_at > datetime.now(UTC).replace(tzinfo=None),
    ).with_for_update(of=RegistrationToken))).one_or_none()
    if row is None:
        return None
    token = row.RegistrationToken
    app_id = token.app_id
    if not row.estate_active:
        return None
    if not row.host_roles or UserRole.LANDLORD not in row.host_roles:
        return None
    # A public invitation must never change or relink an existing account.
    if row.email_taken:
        return None
    from app.services.auth import require_acceptable_password
    require_acceptable_password(user_in.password)
    db_obj = User(email=email, full_name=user_in.full_name,
        phone=user_in.phone, hashed_password=await get_password_hash_async(user_in.password),
        roles=[UserRole.RESIDENT], house_number=token.house_number,
        street_name=token.street_name, app_id=app_id, landlord_id=token.landlord_id,
        invitation_id=token.id, relationship_type=token.relationship_type)
    db.add(db_obj)
    token.is_used = True
    await db.commit()
    await invalidate_cached_registration_token(app_id, token.code)
    from app.core.redis import invalidate_cached_user
    await invalidate_cached_user(app_id, email)
    # Every serialized field is set above or by a Python-side default, so a refresh
    # (BEGIN + SELECT + ROLLBACK) is only needed when the session expires on commit.
    if db.sync_session.expire_on_commit:
        await db.refresh(db_obj)
    return db_obj

async def get_user(db: AsyncSession, user_id: uuid.UUID, app_id: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.id == user_id, User.app_id == app_id))
    return result.scalar_one_or_none()

async def get_users_by_tenant(db: AsyncSession, app_id: str) -> List[User]:
    result = await db.execute(select(User).where(User.app_id == app_id))
    return list(result.scalars().all())

async def update_user(db: AsyncSession, user_id: uuid.UUID, user_in: UserUpdateSchema, app_id: str):
    db_obj = await db.scalar(select(User).where(
        User.id == user_id, User.app_id == app_id,
    ).with_for_update())
    if not db_obj:
        return None, None

    if db_obj.archived_at is not None:
        from fastapi import HTTPException
        raise HTTPException(409, "Archived accounts cannot be edited")

    update_data = user_in.model_dump(exclude_unset=True)
    reset_password = update_data.pop("reset_password", False)
    if "password" in update_data:
        update_data["hashed_password"] = await get_password_hash_async(update_data.pop("password"))

    old_rfid = db_obj.rfid_tag
    old_email = db_obj.email
    for field, value in update_data.items():
        setattr(db_obj, field, value)
        
    job_status = None
    if reset_password:
        from app.services.account_password_resets import queue_reset
        job = await queue_reset(db, db_obj)
        job_status = {"id": str(job.id), "status": job.status}

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

    if job_status is not None:
        from app.services.worker_wake import wake_worker
        await wake_worker("account_password_reset")

    return db_obj, job_status

async def delete_user(db: AsyncSession, user_id: uuid.UUID, app_id: str):
    db_obj = await get_user(db, user_id, app_id)
    if not db_obj:
        return None

    if db_obj.archived_at is not None:
        from fastapi import HTTPException
        raise HTTPException(409, "Archived accounts are managed by the estate archive")

    email = db_obj.email
    rfid_tag = db_obj.rfid_tag
    # Preserve conversation history and officer attribution when removing an account.
    # These foreign keys otherwise cascade-delete the estate's audit trail.
    from app.models.chat import ChatMessage, ChatThread
    from app.models.onboarding import LandlordOnboardingJob, LandlordImportFile
    from app.models.sos import SOSAlert

    checks = await db.execute(select(
        exists(select(ChatThread.id).where(ChatThread.resident_id == user_id)).label("threads"),
        exists(select(ChatMessage.id).where(ChatMessage.sender_id == user_id)).label("messages"),
        exists(select(User.id).where(User.landlord_id == user_id)).label("household"),
        exists(select(RegistrationToken.id).where(RegistrationToken.landlord_id == user_id)).label("invitations"),
        exists(select(LandlordOnboardingJob.id).where(LandlordOnboardingJob.user_id == user_id)).label("onboarding"),
        exists(select(LandlordImportFile.id).where(LandlordImportFile.uploaded_by == user_id)).label("uploads"),
        exists(select(SOSAlert.id).where(SOSAlert.sender_id == user_id)).label("sos"),
    ))
    has_history = any(checks.one())

    if has_history:
        db_obj.is_revoked = True
        db_obj.fcm_token = None
        action = "revoked"
    else:
        await db.delete(db_obj)
        action = "deleted"
    await db.commit()

    from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user
    await invalidate_cached_user(app_id, email)
    await invalidate_cached_rfid_user(app_id, rfid_tag)

    return action

async def update_user_fcm_token(db: AsyncSession, user_id: uuid.UUID, fcm_token: str, *, active_session: str | None = None) -> bool:
    """Updates and persists the FCM device push notification token for the user."""
    user = await db.get(User, user_id, with_for_update=True, populate_existing=True)
    if not user or (active_session is not None and user.active_session_id != active_session):
        return False
    from sqlalchemy.dialects.postgresql import insert
    from app.models.sos import PushDevice

    await db.execute(delete(PushDevice).where(
        PushDevice.user_id == user_id, PushDevice.token != fcm_token))
    await db.execute(insert(PushDevice).values(token=fcm_token, user_id=user_id)
                     .on_conflict_do_update(index_elements=[PushDevice.token], set_={"user_id": user_id}))
    # A physical device must stop receiving alerts for its previous account.
    from sqlalchemy import update
    await db.execute(update(User).where(User.fcm_token==fcm_token,User.id!=user_id).values(fcm_token=None))
    user.fcm_token = fcm_token
    await db.commit()
    await db.refresh(user)
    return True
