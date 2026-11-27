"""Estate caretakers register and manage security personnel (gate officers).

Only a caretaker can create a security account, so every officer is registered and
vouched for by the estate. A new officer gets a temporary password (shown once to the
caretaker and emailed by the worker) and must choose their own password before any
endpoint, including gate verification, accepts their requests. Suspending an officer
(PATCH /users/{id}/status) takes effect on their next request.

Welcome and reset emails use the same durable credential job as directory resets
(`account_password_resets`): the temporary password starts working only once Resend
accepts the email, and failed sends retry until they succeed.

Caretakers can correct an officer's name, email and phone and keep employment details
(staff ID, alternate phone, home address, emergency contact, notes). Every change is
written to the admin audit log.
"""
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import invalidate_cached_user
from app.dependencies import check_roles, validate_tenant
from app.models.admin_audit import AdminAuditLog
from app.models.onboarding import AccountPasswordResetJob
from app.models.user import SecurityStaffProfile, User, UserRole
from app.services import account_password_resets as credentials
from app.services.auth import clear_session_devices, get_password_hash_async
from app.services.worker_wake import wake_worker

router = APIRouter()

PROFILE_FIELDS = ("staff_number", "alternate_phone", "home_address",
                  "emergency_contact_name", "emergency_contact_phone", "notes")
EMAIL_TAKEN = "This email already belongs to an account in your estate."


class StaffDetails(BaseModel):
    """Optional details shared by registration and editing. Blank text clears a field."""
    phone: Optional[str] = Field(None, max_length=40)
    staff_number: Optional[str] = Field(None, max_length=50)
    alternate_phone: Optional[str] = Field(None, max_length=40)
    home_address: Optional[str] = Field(None, max_length=500)
    emergency_contact_name: Optional[str] = Field(None, max_length=255)
    emergency_contact_phone: Optional[str] = Field(None, max_length=40)
    notes: Optional[str] = Field(None, max_length=1000)

    @field_validator("*", mode="before")
    @classmethod
    def strip(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class SecurityStaffCreate(StaffDetails):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr


class SecurityStaffUpdate(StaffDetails):
    full_name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None


class SecurityStaffView(BaseModel):
    id: uuid.UUID
    full_name: Optional[str]
    email: str
    phone: Optional[str]
    status: str  # active | awaiting_first_sign_in | suspended
    created_at: datetime
    # Latest sign-in email: queued | retrying | sent | cancelled, or None when none was sent.
    email_status: Optional[str] = None
    staff_number: Optional[str] = None
    alternate_phone: Optional[str] = None
    home_address: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    notes: Optional[str] = None


class SecurityStaffCredentials(SecurityStaffView):
    temporary_password: str
    message: str


class SecurityStaffUpdated(SecurityStaffView):
    temporary_password: Optional[str] = None  # set when a changed email got a new welcome
    message: str


def _view(user: User, email_status: Optional[str] = None, profile: Optional[SecurityStaffProfile] = None) -> dict:
    state = "suspended" if user.is_revoked else ("awaiting_first_sign_in" if user.must_change_password else "active")
    return dict(id=user.id, full_name=user.full_name, email=user.email, phone=user.phone,
                status=state, created_at=user.created_at, email_status=email_status,
                **{field: getattr(profile, field, None) for field in PROFILE_FIELDS})


async def _issue(db: AsyncSession, user: User, kind: str, message: str,
                 profile: Optional[SecurityStaffProfile] = None) -> dict:
    """Queue the officer's credential email, commit, wake the worker, and show the password once."""
    job = await credentials.queue_reset(db, user, kind=kind)
    password = credentials.reveal_password(job)
    await db.commit()
    await invalidate_cached_user(user.app_id, user.email)
    await wake_worker("credential_email")
    return {**_view(user, job.status, profile), "temporary_password": password, "message": message}


async def _officer(db: AsyncSession, user_id: uuid.UUID, app_id: str) -> User:
    user = (await db.execute(select(User).where(User.id == user_id, User.app_id == app_id)
                             .with_for_update())).scalar_one_or_none()
    if not user or UserRole.SECURITY not in user.roles:
        raise HTTPException(404, "Security officer not found")
    return user


async def _email_taken(db: AsyncSession, app_id: str, email: str, except_id: Optional[uuid.UUID] = None) -> bool:
    query = select(User.id).where(func.lower(User.email) == email, User.app_id == app_id)
    if except_id is not None:
        query = query.where(User.id != except_id)
    return await db.scalar(query.limit(1)) is not None


@router.get("", response_model=List[SecurityStaffView])
async def list_security_staff(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    rows = (await db.execute(select(User).where(User.app_id == app_id, User.roles.contains([UserRole.SECURITY]))
                             .order_by(User.created_at.desc()))).scalars().all()
    if not rows:
        return []
    ids = [u.id for u in rows]
    latest = dict((await db.execute(
        select(AccountPasswordResetJob.user_id, AccountPasswordResetJob.status)
        .where(AccountPasswordResetJob.app_id == app_id, AccountPasswordResetJob.user_id.in_(ids))
        .order_by(AccountPasswordResetJob.user_id, AccountPasswordResetJob.created_at.desc(),
                  AccountPasswordResetJob.id.desc())
        .distinct(AccountPasswordResetJob.user_id)
    )).all())
    profiles = {p.user_id: p for p in (await db.execute(select(SecurityStaffProfile).where(
        SecurityStaffProfile.app_id == app_id, SecurityStaffProfile.user_id.in_(ids)))).scalars()}
    return [_view(u, latest.get(u.id), profiles.get(u.id)) for u in rows]


@router.post("", response_model=SecurityStaffCredentials, status_code=status.HTTP_201_CREATED)
async def register_security_staff(body: SecurityStaffCreate, app_id: str = Depends(validate_tenant),
                                  db: AsyncSession = Depends(get_db),
                                  current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    email = str(body.email).strip().lower()
    if await _email_taken(db, app_id, email):
        raise HTTPException(409, EMAIL_TAKEN)
    # Nobody knows this placeholder; the emailed temporary password replaces it on delivery.
    user = User(email=email, full_name=body.full_name, phone=body.phone, app_id=app_id,
                roles=[UserRole.SECURITY], must_change_password=True,
                hashed_password=await get_password_hash_async(credentials.unusable_password()))
    db.add(user)
    await db.flush()
    profile = SecurityStaffProfile(app_id=app_id, user_id=user.id,
                                   **{field: getattr(body, field) for field in PROFILE_FIELDS})
    db.add(profile)
    return await _issue(db, user, "staff_welcome",
                        "Security officer registered. This password works once the email is sent, "
                        "usually within a minute. They must set their own password at first sign-in.",
                        profile)


@router.patch("/{user_id}", response_model=SecurityStaffUpdated)
async def update_security_staff(user_id: uuid.UUID, body: SecurityStaffUpdate,
                                app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    user = await _officer(db, user_id, app_id)
    if user.archived_at is not None:
        raise HTTPException(409, "Archived accounts cannot be edited")
    changes = body.model_dump(exclude_unset=True)
    for required in ("full_name", "email"):
        if required in changes and changes[required] is None:
            raise HTTPException(422, f"{required.replace('_', ' ').capitalize()} cannot be empty")

    profile = await db.scalar(select(SecurityStaffProfile).where(SecurityStaffProfile.user_id == user.id))
    if profile is None:
        profile = SecurityStaffProfile(app_id=app_id, user_id=user.id)
        db.add(profile)

    audit: dict = {}

    def apply(target, field, value):
        before = getattr(target, field)
        if before != value:
            setattr(target, field, value)
            audit[field] = {"before": before, "after": value}

    old_email = user.email
    if "email" in changes:
        email = str(changes["email"]).strip().lower()
        if email != user.email:
            if await _email_taken(db, app_id, email, except_id=user.id):
                raise HTTPException(409, EMAIL_TAKEN)
            apply(user, "email", email)
    for field in ("full_name", "phone"):
        if field in changes:
            apply(user, field, changes[field])
    for field in PROFILE_FIELDS:
        if field in changes:
            apply(profile, field, changes[field])

    email_changed = user.email != old_email
    if email_changed:
        # Sessions are tied to the sign-in email: the officer signs in again with the new one.
        user.active_session_id = None
        await clear_session_devices(db, user)
    if audit:
        db.add(AdminAuditLog(actor_id=current_user.id, actor_email=current_user.email,
                             action="security_staff.update", target_type="user", target_id=str(user.id),
                             changes={"app_id": app_id, **audit}))

    try:
        if email_changed and user.must_change_password and not user.is_revoked:
            # They never signed in, so the old welcome went to the wrong address: send a new one.
            result = await _issue(db, user, "staff_welcome",
                                  "Details saved. A new welcome email is going to the updated address; this "
                                  "password works once it is sent. The previous temporary password no longer applies.",
                                  profile)
        else:
            await db.commit()
            await invalidate_cached_user(app_id, user.email)
            message = ("Details saved. The officer now signs in with the new email address."
                       if email_changed else "Details saved." if audit else "No changes to save.")
            result = {**_view(user, None, profile), "message": message}
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, EMAIL_TAKEN)
    if email_changed:
        await invalidate_cached_user(app_id, old_email)
    if result.get("email_status") is None:
        result["email_status"] = await db.scalar(
            select(AccountPasswordResetJob.status).where(AccountPasswordResetJob.user_id == user.id)
            .order_by(AccountPasswordResetJob.created_at.desc(), AccountPasswordResetJob.id.desc()).limit(1))
    return result


@router.post("/{user_id}/reset-password", response_model=SecurityStaffCredentials)
async def reset_security_staff_password(user_id: uuid.UUID, app_id: str = Depends(validate_tenant),
                                        db: AsyncSession = Depends(get_db),
                                        current_user: User = Depends(check_roles([UserRole.CARETAKER]))):
    user = await _officer(db, user_id, app_id)
    if user.is_revoked:
        raise HTTPException(409, "Reinstate this officer before resetting their password")
    profile = await db.scalar(select(SecurityStaffProfile).where(SecurityStaffProfile.user_id == user.id))
    return await _issue(db, user, "reset",
                        "New temporary password issued. It replaces the current password once the email "
                        "is sent, usually within a minute. The officer must set their own password at sign-in.",
                        profile)
