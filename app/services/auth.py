import asyncio
from datetime import datetime, timedelta, UTC
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from app.models.user import User
import uuid

import hmac
import hashlib

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Regular Tenant User Auth ---

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

# bcrypt costs ~200-400 ms of CPU. Called inline it freezes the event loop, stalling
# every other request on the worker. Request handlers must use these async variants,
# which run bcrypt in a thread (bcrypt releases the GIL while hashing).
async def verify_password_async(plain_password, hashed_password) -> bool:
    return await asyncio.to_thread(verify_password, plain_password, hashed_password)

async def get_password_hash_async(password) -> str:
    return await asyncio.to_thread(get_password_hash, password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    to_encode.setdefault("iat", datetime.now(UTC).timestamp())
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

# --- SaaS Platform Admin Isolated Auth ---

def _pepper_admin_password(password: str) -> str:
    """Pre-hashes admin password using HMAC-SHA256 with dedicated SAAS_ADMIN_PASSWORD_PEPPER."""
    return hmac.new(
        settings.SAAS_ADMIN_PASSWORD_PEPPER.encode("utf-8"),
        password.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def get_saas_admin_password_hash(password: str) -> str:
    """Hashes a SaaS Admin password using dedicated HMAC pepper + bcrypt."""
    peppered = _pepper_admin_password(password)
    return pwd_context.hash(peppered)

def verify_saas_admin_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a SaaS Admin password using dedicated HMAC pepper."""
    peppered = _pepper_admin_password(plain_password)
    return pwd_context.verify(peppered, hashed_password)

async def verify_saas_admin_password_async(plain_password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(verify_saas_admin_password, plain_password, hashed_password)

async def get_saas_admin_password_hash_async(password: str) -> str:
    return await asyncio.to_thread(get_saas_admin_password_hash, password)

def create_saas_admin_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Creates a JWT access token for SaaS Admin signed with the dedicated SAAS_ADMIN_SECRET_KEY.
    Cannot be decoded or forged by regular tenant SECRET_KEY.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "scope": "saas_admin"})
    encoded_jwt = jwt.encode(to_encode, settings.SAAS_ADMIN_SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

async def authenticate_user(db: AsyncSession, email: str, password: str, app_id: str) -> Optional[User]:
    # Phone keyboards often capitalise the first letter, so match the email without
    # regard to case. Prefer an exact match; if an estate holds two accounts that differ
    # only by case (older registrations kept the typed case), never guess between them.
    typed = email.strip()
    candidates = (await db.execute(
        select(User).where(func.lower(User.email) == typed.lower(), User.app_id == app_id)
    )).scalars().all()
    exact = [u for u in candidates if u.email == typed]
    user = exact[0] if exact else (candidates[0] if len(candidates) == 1 else None)
    if not user:
        return None
    if not await verify_password_async(password, user.hashed_password):
        return None
    return user



def token_matches_user(payload: dict, user: User) -> bool:
    """Bind sessions to immutable identity; reject unsafe legacy email-only tokens."""
    if payload.get("uid") is not None:
        return str(user.id) == payload["uid"]
    try:
        created = user.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return created.timestamp() <= float(payload["iat"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
