import uuid
from datetime import datetime, UTC, timedelta
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.core.redis import invalidate_cached_user, invalidate_cached_rfid_user, blacklist_token
from app.models.user import User, UserRole
from app.models.token import RegistrationToken
from app.models.estate import Estate
from app.services.auth import verify_password, create_access_token


async def update_profile(db, current_user, profile, token):
    user = await db.get(User, current_user.id, populate_existing=True)
    if not user or user.is_revoked:
        raise HTTPException(401, 'Your account is unavailable')
    changes = profile.model_dump(exclude_unset=True, exclude={'current_password'})
    new_email = str(profile.email).lower() if profile.email is not None else user.email
    if 'email' in changes and profile.email is None:
        raise HTTPException(422, 'Email cannot be empty')
    if 'full_name' in changes and not profile.full_name:
        raise HTTPException(422, 'Enter your full name')
    old_email = user.email
    if new_email != old_email:
        if not profile.current_password or len(profile.current_password.encode("utf-8"))>72 or not verify_password(profile.current_password, user.hashed_password):
            raise HTTPException(400, 'Enter your current password to change your email')
        exists = await db.scalar(select(User.id).where(User.email == new_email, User.id != user.id))
        if exists:
            raise HTTPException(409, 'This email address is already in use')
        changes['email'] = new_email
    for field, value in changes.items():
        setattr(user, field, value)
    household=[]
    try:
        if UserRole.LANDLORD in user.roles and {'house_number', 'street_name'} & changes.keys():
            household=(await db.execute(select(User.email,User.rfid_tag).where(User.landlord_id==user.id,User.app_id==user.app_id))).all()
            address = {'house_number': user.house_number, 'street_name': user.street_name}
            await db.execute(update(User).where(User.landlord_id == user.id, User.app_id == user.app_id).values(**address))
            await db.execute(update(RegistrationToken).where(RegistrationToken.landlord_id == user.id, RegistrationToken.app_id == user.app_id, RegistrationToken.is_used.is_(False), RegistrationToken.is_revoked.is_(False)).values(**address))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, 'These profile details conflict with an existing account')
    await db.refresh(user)
    await invalidate_cached_user(user.app_id, old_email)
    await invalidate_cached_user(user.app_id, user.email)
    await invalidate_cached_rfid_user(user.app_id, user.rfid_tag)
    for email,rfid in household:
        await invalidate_cached_user(user.app_id,email)
        await invalidate_cached_rfid_user(user.app_id,rfid)
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    remaining = max(1, claims['exp'] - datetime.now(UTC).timestamp())
    await blacklist_token(token, ttl_seconds=int(remaining) + 1)
    fresh = create_access_token({'sub': user.email, 'uid': str(user.id), 'app_id': user.app_id, 'roles': [r.value for r in user.roles], 'purpose': 'access', 'jti': str(uuid.uuid4())}, expires_delta=timedelta(seconds=remaining))
    length = await db.scalar(select(Estate.token_code_length).where(Estate.app_id == user.app_id))
    return {'token': {'access_token': fresh, 'token_type': 'bearer'}, 'user': user, 'token_code_length': length or 6}
