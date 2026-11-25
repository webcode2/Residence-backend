"""Estate caretakers register and manage security personnel (gate officers).

Only a caretaker can create a security account, so every officer is registered and
vouched for by the estate. A new officer gets a temporary password (shown once to the
caretaker and emailed by the worker) and must choose their own password before any
endpoint, including gate verification, accepts their requests. Suspending an officer
(PATCH /users/{id}/status) takes effect on their next request.
"""
import secrets
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import invalidate_cached_user
from app.dependencies import check_roles, validate_tenant
from app.models.user import User, UserRole
from app.services import notification_queue
from app.services.auth import get_password_hash_async

router = APIRouter()


class SecurityStaffCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=40)

    @field_validator("full_name", "phone", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class SecurityStaffView(BaseModel):
    id: uuid.UUID
    full_name: Optional[str]
    email: str
    phone: Optional[str]
    status: str  # active | awaiting_first_sign_in | suspended
    created_at: datetime


class SecurityStaffCredentials(SecurityStaffView):
    temporary_password: str
    email_status: str = "queued"
    message: str


def _view(user: User) -> dict:
    state = "suspended" if user.is_revoked else ("awaiting_first_sign_in" if user.must_change_password else "active")
    return dict(id=user.id, full_name=user.full_name, email=user.email, phone=user.phone,
                status=state, created_at=user.created_at)


async def _queue_welcome(user: User, password: str) -> None:
    from app.services.landlords import cipher
    await notification_queue.dispatch(
        "staff_welcome", email=user.email, full_name=user.full_name or "", app_id=user.app_id,
        password_ciphertext=cipher().encrypt(password.encode()).decode(),
        idempotency_key=f"staff-welcome-{user.id}-{secrets.token_hex(4)}")


@router.get("", response_model=List[SecurityStaffView])
async def list_security_staff(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    rows = (await db.execute(select(User).where(User.app_id == app_id, User.roles.contains([UserRole.SECURITY]))
                             .order_by(User.created_at.desc()))).scalars().all()
    return [_view(u) for u in rows]


@router.post("", response_model=SecurityStaffCredentials, status_code=status.HTTP_201_CREATED)
async def register_security_staff(body: SecurityStaffCreate, app_id: str = Depends(validate_tenant),
                                  db: AsyncSession = Depends(get_db),
                                  current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    email = str(body.email).strip().lower()
    if await db.scalar(select(User.id).where(User.email == email, User.app_id == app_id)):
        raise HTTPException(409, "This email already belongs to an account in your estate.")
    password = secrets.token_urlsafe(10)
    user = User(email=email, full_name=body.full_name, phone=body.phone or None, app_id=app_id,
                roles=[UserRole.SECURITY], must_change_password=True,
                hashed_password=await get_password_hash_async(password))
    db.add(user)
    await db.commit()
    await invalidate_cached_user(app_id, email)
    await _queue_welcome(user, password)
    return {**_view(user), "temporary_password": password,
            "message": "Security officer registered. They must set their own password at first sign-in."}


@router.post("/{user_id}/reset-password", response_model=SecurityStaffCredentials)
async def reset_security_staff_password(user_id: uuid.UUID, app_id: str = Depends(validate_tenant),
                                        db: AsyncSession = Depends(get_db),
                                        current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    user = (await db.execute(select(User).where(User.id == user_id, User.app_id == app_id))).scalar_one_or_none()
    if not user or UserRole.SECURITY not in user.roles:
        raise HTTPException(404, "Security officer not found")
    if user.is_revoked:
        raise HTTPException(409, "Reinstate this officer before resetting their password")
    password = secrets.token_urlsafe(10)
    user.hashed_password = await get_password_hash_async(password)
    user.must_change_password = True
    await db.commit()
    await invalidate_cached_user(app_id, user.email)
    await _queue_welcome(user, password)
    return {**_view(user), "temporary_password": password,
            "message": "New temporary password issued. The officer must set their own password at sign-in."}
