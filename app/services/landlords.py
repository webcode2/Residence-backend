"""Persistent onboarding queue. Passwords are encrypted until email acceptance."""
import asyncio
import base64
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from sqlalchemy import select, or_, and_
from sqlalchemy.dialects.postgresql import insert
from cryptography.fernet import Fernet
from app.models.base import utc_now
from app.models.onboarding import LandlordOnboardingJob as Job
from app.models.user import User, UserRole
from app.schemas.landlord import LandlordImportResponseSchema
from app.core.config import settings
from app.services.auth import get_password_hash
from app.services.notifications import notification_service

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5

def cipher():
    # Domain-separated key derived from the deployment secret; never log/decrypt in APIs.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('landlord-onboarding:'+settings.SECRET_KEY).encode()).digest()))

async def import_landlords(db, import_data, app_id, batch_id=None):
    batch_id = batch_id or uuid.uuid4()
    results = []
    for entry in import_data:
        email = str(entry.email).strip().lower()
        job_id = uuid.uuid4()
        account_exists = (await db.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
        values = dict(id=job_id, batch_id=batch_id, app_id=app_id, email=email,
            full_name=entry.name.strip(), phone=entry.phone.strip(), house_number=entry.house_number.strip(),
            street_name=entry.street_name, status='skipped' if account_exists else 'queued', attempts=0,
            created_at=utc_now(), updated_at=utc_now(), next_attempt_at=utc_now(),
            last_error='Account already exists; no account or password changed' if account_exists else None)
        inserted = (await db.execute(insert(Job).values(**values).on_conflict_do_nothing(
            constraint='uq_landlord_onboarding_estate_email').returning(Job.id))).scalar_one_or_none()
        if not inserted:
            existing = (await db.execute(select(Job).where(Job.app_id == app_id, Job.email == email))).scalar_one()
            results.append(LandlordImportResponseSchema(email=email, status='skipped',
                message='Already tracked; use the job retry action for failed jobs', job_id=str(existing.id), batch_id=str(existing.batch_id)))
        else:
            results.append(LandlordImportResponseSchema(email=email, status=values['status'],
                message=values['last_error'] or 'Queued for account creation and welcome email', job_id=str(job_id), batch_id=str(batch_id)))
    await db.commit()
    return results

async def process_one(db):
    now = utc_now()
    job = (await db.execute(select(Job).where(or_(
        and_(Job.status.in_(['queued','retrying']), Job.next_attempt_at <= now),
        and_(Job.status == 'processing', Job.lease_until < now),
    )).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
    if not job:
        await db.rollback()
        return False
    if job.attempts >= MAX_ATTEMPTS:
        job.status = 'failed'
        job.lease_until = None
        job.last_error = 'Processing was interrupted repeatedly. Retry the job after checking the worker.'
        await db.commit()
        return True
    job.status = 'processing'
    job.attempts += 1
    job.lease_until = now + timedelta(minutes=2)
    job_id = job.id
    await db.commit()
    try:
        # Reacquire and hold the row through provider delivery, so lease reclamation
        # cannot run concurrently with a slow attempt.
        job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
        if job.user_id is None:
            existing = (await db.execute(select(User).where(User.email == job.email))).scalar_one_or_none()
            if existing:
                job.status = 'skipped'
                job.last_error = 'Account already exists; no account or password changed'
                job.lease_until = None
                await db.commit()
                return True
            password = secrets.token_urlsafe(18)
            job.password_ciphertext = cipher().encrypt(password.encode()).decode()
            user = User(email=job.email, full_name=job.full_name, phone=job.phone,
                house_number=job.house_number, street_name=job.street_name, app_id=job.app_id,
                roles=[UserRole.LANDLORD], must_change_password=True,
                hashed_password=await asyncio.to_thread(get_password_hash,password))
            db.add(user)
            await db.flush()
            job.user_id = user.id
            # Persist the account and encrypted credential before sending. A crash
            # retries the same credential and provider idempotency key.
            await db.commit()
            job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
        account = await db.get(User, job.user_id)
        if account and not account.must_change_password:
            job.status = 'sent'
            job.password_ciphertext = None
            job.lease_until = None
            job.last_error = None
            await db.commit()
            return True
        password = cipher().decrypt(job.password_ciphertext.encode()).decode()
        accepted = await notification_service.send_registration_email(
            email=job.email, password=password, full_name=job.full_name, app_id=job.app_id,
            idempotency_key='landlord-welcome-'+str(job.id))
        if not accepted:
            raise RuntimeError('Welcome email provider did not accept the message; verify email configuration and retry')
        job.status = 'sent'
        job.last_error = None
        job.password_ciphertext = None
        job.lease_until = None
        job.next_attempt_at = None
        await db.commit()
    except Exception:
        await db.rollback()
        job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
        job.status = 'failed' if job.attempts >= MAX_ATTEMPTS else 'retrying'
        job.last_error = 'Account or welcome email processing failed. Check server/email configuration, then retry.'
        job.next_attempt_at = utc_now() + timedelta(seconds=min(3600, 30 * 2 ** min(job.attempts, 7)))
        job.lease_until = None
        await db.commit()
        logger.warning('Landlord onboarding job %s failed on attempt %s', job.id, job.attempts)
    return True

_task = None
async def _run():
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
            logger.exception('Onboarding queue worker unavailable')
            await asyncio.sleep(5)

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
