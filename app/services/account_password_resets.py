"""Durable, estate-scoped delivery of temporary account passwords.

One job type carries every credential email for an existing estate account:
`staff_welcome` (a caretaker registered a security officer) and `reset` (a caretaker
reset a password from the directory or the Security staff page). The worker sends the
email and only after Resend accepts it does the temporary password become the account
password, so a failed email never locks anyone out. Failed sends retry with backoff.
"""
import asyncio
import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import invalidate_cached_user
from app.models.base import utc_now
from app.models.onboarding import AccountPasswordResetJob
from app.models.user import User
from app.services.auth import get_password_hash_async
from app.services.landlords import cipher
from app.services.notifications import notification_service

logger = logging.getLogger(__name__)
PENDING_STATUSES = ("queued", "retrying")
KINDS = ("reset", "staff_welcome")


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()


def temporary_password() -> str:
    return secrets.token_urlsafe(12)


def unusable_password() -> str:
    """Placeholder secret for a new account until its welcome email is accepted."""
    return secrets.token_urlsafe(32)


def reveal_password(job: AccountPasswordResetJob) -> str | None:
    """The pending temporary password, for the caretaker's one-time view."""
    if not job.password_ciphertext:
        return None
    return cipher().decrypt(job.password_ciphertext.encode()).decode()


async def queue_reset(db: AsyncSession, user: User, kind: str = "reset") -> AccountPasswordResetJob:
    """Queue a credential email; reuses a pending job for the same account and password."""
    if kind not in KINDS:
        raise ValueError(f"unknown credential email kind {kind!r}")
    if user.is_revoked or user.archived_at is not None:
        raise HTTPException(409, "Restore this account before resetting its password")
    fingerprint = _fingerprint(user.hashed_password)
    existing = await db.scalar(select(AccountPasswordResetJob).where(
        AccountPasswordResetJob.user_id == user.id,
        AccountPasswordResetJob.email == user.email,
        AccountPasswordResetJob.previous_password_fingerprint == fingerprint,
        AccountPasswordResetJob.status.in_(PENDING_STATUSES),
    ).limit(1))
    if existing is not None:
        return existing
    job = AccountPasswordResetJob(
        app_id=user.app_id, user_id=user.id, email=user.email, kind=kind,
        previous_password_fingerprint=fingerprint,
        password_ciphertext=cipher().encrypt(temporary_password().encode()).decode(),
        status="queued", next_attempt_at=utc_now(),
    )
    db.add(job)
    await db.flush()
    return job


async def process_one(db: AsyncSession) -> bool:
    # Both the producer and worker serialize on the user row. Keep it stable
    # through provider acceptance so a stale job cannot activate a credential.
    result = await db.execute(
        select(AccountPasswordResetJob, User)
        .join(User, User.id == AccountPasswordResetJob.user_id)
        .where(AccountPasswordResetJob.status.in_(PENDING_STATUSES),
               AccountPasswordResetJob.next_attempt_at <= utc_now())
        .order_by(AccountPasswordResetJob.next_attempt_at, AccountPasswordResetJob.id)
        .limit(1)
        .with_for_update(of=(User, AccountPasswordResetJob), skip_locked=True)
    )
    row = result.one_or_none()
    if row is None:
        return False
    job, user = row
    if (user.app_id != job.app_id or user.email != job.email or user.is_revoked
            or user.archived_at is not None
            or _fingerprint(user.hashed_password) != job.previous_password_fingerprint):
        job.status = "cancelled"
        job.password_ciphertext = None
        job.next_attempt_at = None
        await db.commit()
        return True

    job.attempts += 1
    try:
        temporary = reveal_password(job)
        password_hash = await get_password_hash_async(temporary)
        delivered = await notification_service.send_credential_email(
            job.kind, job.email, temporary, user.full_name or "", job.app_id,
            idempotency_key=f"account-reset-{job.id}",
        )
    except Exception:
        logger.exception("Credential email job %s failed", job.id)
        delivered = False
    if not delivered:
        job.status = "retrying"
        job.next_attempt_at = utc_now() + timedelta(seconds=min(3600, 30 * 2 ** min(job.attempts, 7)))
        await db.commit()
        logger.warning("Credential email job %s (%s) will retry at %s", job.id, job.kind, job.next_attempt_at)
        return True

    user.hashed_password = password_hash
    user.must_change_password = True
    job.status = "sent"
    job.password_ciphertext = None
    job.next_attempt_at = None
    app_id, email = user.app_id, user.email
    await db.commit()
    await invalidate_cached_user(app_id, email)
    return True


async def _run():
    """Embedded / standalone polling loop for deployments without the woken worker."""
    from app.core.database import SessionLocal
    while True:
        try:
            async with SessionLocal() as db:
                worked = await process_one(db)
            if not worked:
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Credential email queue unavailable")
            await asyncio.sleep(5)


_task = None


def start_worker():
    global _task
    _task = asyncio.create_task(_run())


async def stop_worker():
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
