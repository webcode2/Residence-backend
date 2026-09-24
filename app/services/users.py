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

async def register_resident(db: AsyncSession, user_in: UserCreateSchema, registration_code: str, app_id: str) -> Optional[User]:
    """Register a resident using a registration token."""
    # Verify token
    token_result = await db.execute(
        select(RegistrationToken).where(
            RegistrationToken.code == registration_code,
            RegistrationToken.app_id == app_id,
            RegistrationToken.is_used == False,
            RegistrationToken.is_revoked == False
        )
    )
    token = token_result.scalar_one_or_none()
    if not token:
        return None
    
    # Create or update resident
    user_result = await db.execute(select(User).where(User.email == user_in.email, User.app_id == app_id))
    db_obj = user_result.scalar_one_or_none()
    
    from app.models.user import UserRole
    
    if db_obj:
        # Existing user: Add resident role if missing
        if UserRole.RESIDENT not in db_obj.roles:
            new_roles = list(db_obj.roles)
            new_roles.append(UserRole.RESIDENT)
            db_obj.roles = new_roles
            if not db_obj.landlord_id:
                db_obj.landlord_id = token.landlord_id
                db_obj.house_number = token.house_number
                db_obj.street_name = token.street_name
    else:
        # New user
        db_obj = User(
            email=user_in.email,
            full_name=user_in.full_name,
            hashed_password=get_password_hash(user_in.password),
            roles=[UserRole.RESIDENT],
            rfid_tag=user_in.rfid_tag,
            house_number=token.house_number,
            street_name=token.street_name,
            app_id=app_id,
            landlord_id=token.landlord_id
        )
        db.add(db_obj)
    
    # Mark token as used
    token.is_used = True
    
    await db.commit()
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
    for field, value in update_data.items():
        setattr(db_obj, field, value)
        
    await db.commit()
    await db.refresh(db_obj)

    # Invalidate Redis user session + RFID caches
    from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user
    await invalidate_cached_user(app_id, db_obj.email)
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
    await db.delete(db_obj)
    await db.commit()

    from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user
    await invalidate_cached_user(app_id, email)
    await invalidate_cached_rfid_user(app_id, rfid_tag)

    return True
