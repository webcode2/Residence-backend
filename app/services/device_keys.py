"""Fingerprint / Face ID sign-in for residents (one active phone per account).

Enrolling returns a random secret once; the phone stores it behind a biometric check
and sends it to sign in. Security officers are excluded: the gate app has no app lock
or biometric sign-in.
"""
import hashlib
import hmac
import secrets
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utc_now
from app.models.user import DeviceSignInKey, User, UserRole


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def eligible(user: User) -> bool:
    return (UserRole.SECURITY not in user.roles and UserRole.SAAS_OWNER not in user.roles
            and not user.is_revoked and user.archived_at is None and not user.must_change_password)


async def enrol(db: AsyncSession, user: User, device_label: str | None) -> tuple[DeviceSignInKey, str]:
    """Issue a new key for this phone; any earlier key for the account stops working."""
    await db.execute(update(DeviceSignInKey).where(
        DeviceSignInKey.user_id == user.id, DeviceSignInKey.revoked_at.is_(None),
    ).values(revoked_at=utc_now()))
    secret = secrets.token_urlsafe(32)
    key = DeviceSignInKey(app_id=user.app_id, user_id=user.id, secret_hash=_sha256(secret),
                          password_fingerprint=_sha256(user.hashed_password),
                          device_label=(device_label or "").strip()[:100] or None)
    db.add(key)
    await db.flush()
    return key, secret


async def authenticate(db: AsyncSession, app_id: str, key_id: uuid.UUID, secret: str) -> User | None:
    """The account behind a valid key, locked for update; revokes keys that no longer apply."""
    row = (await db.execute(
        select(DeviceSignInKey, User).join(User, User.id == DeviceSignInKey.user_id)
        .where(DeviceSignInKey.id == key_id, DeviceSignInKey.app_id == app_id)
        .with_for_update(of=(DeviceSignInKey, User))
    )).one_or_none()
    if row is None:
        return None
    key, user = row
    if key.revoked_at is not None or not hmac.compare_digest(key.secret_hash, _sha256(secret)):
        return None
    if user.app_id != app_id or not eligible(user) or \
            not hmac.compare_digest(key.password_fingerprint, _sha256(user.hashed_password)):
        key.revoked_at = utc_now()  # password changed, suspended, archived or now a guard
        await db.commit()
        return None
    key.last_used_at = utc_now()
    return user


async def revoke(db: AsyncSession, user: User, key_id: uuid.UUID) -> bool:
    result = await db.execute(update(DeviceSignInKey).where(
        DeviceSignInKey.id == key_id, DeviceSignInKey.user_id == user.id,
        DeviceSignInKey.revoked_at.is_(None),
    ).values(revoked_at=utc_now()).returning(DeviceSignInKey.id))
    return result.scalar_one_or_none() is not None
