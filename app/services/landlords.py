"""Persistent onboarding queue. Passwords are encrypted until email acceptance."""
import asyncio
import base64
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, update, func, or_, and_
from sqlalchemy.dialects.postgresql import insert
from cryptography.fernet import Fernet
from app.models.base import utc_now
from app.models.onboarding import LandlordOnboardingJob as Job, LandlordImportFile
from app.models.user import User, UserRole
from app.schemas.landlord import LandlordImportResponseSchema
from app.core.config import settings
from app.services.auth import get_password_hash
from app.services.notifications import notification_service

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
BATCH_PAGE_SIZE = 20
RESUMABLE = ['failed', 'retrying']

def cipher():
    # Domain-separated key derived from the deployment secret; never log/decrypt in APIs.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('landlord-onboarding:'+settings.SECRET_KEY).encode()).digest()))

async def import_landlords(db, import_data, app_id, batch_id=None):
    batch_id = batch_id or uuid.uuid4()
    results = []
    for entry in import_data:
        email = str(entry.email).strip().lower()
        job_id = uuid.uuid4()
        account_exists = (await db.execute(select(User.id).where(User.email == email, User.app_id == app_id))).scalar_one_or_none()
        values = dict(id=job_id, batch_id=batch_id, app_id=app_id, email=email,
            full_name=entry.name.strip(), phone=entry.phone.strip(), house_number=entry.house_number.strip(),
            street_name=entry.street_name, status='skipped' if account_exists else 'queued', attempts=0,
            created_at=utc_now(), updated_at=utc_now(), next_attempt_at=utc_now(),
            last_error='Account already exists; no account or password changed' if account_exists else None)
        inserted = (await db.execute(insert(Job).values(**values).on_conflict_do_nothing(
            constraint='uq_landlord_onboarding_estate_email').returning(Job.id))).scalar_one_or_none()
        if not inserted:
            existing = (await db.execute(select(Job).where(Job.app_id == app_id, Job.email == email))).scalar_one()
            # Uploading again continues unfinished rows; rows without an account take the corrected details.
            details = {} if existing.user_id else dict(full_name=values['full_name'], phone=values['phone'],
                house_number=values['house_number'], street_name=values['street_name'])
            resumed = (await db.execute(update(Job).where(Job.id == existing.id, Job.status.in_(RESUMABLE)).values(
                batch_id=batch_id, created_at=utc_now(), **_requeued(), **details).returning(Job.id))).scalar_one_or_none()
            if resumed:
                results.append(LandlordImportResponseSchema(email=email, status='requeued',
                    message='Continuing this unfinished row', job_id=str(resumed), batch_id=str(batch_id)))
                continue
            results.append(LandlordImportResponseSchema(email=email, status='skipped',
                message='Already imported or in progress', job_id=str(existing.id), batch_id=str(existing.batch_id)))
        else:
            results.append(LandlordImportResponseSchema(email=email, status=values['status'],
                message=values['last_error'] or 'Queued for account creation and welcome email', job_id=str(job_id), batch_id=str(batch_id)))
    await db.commit()
    return results

def _requeued():
    return dict(status='queued', attempts=0, next_attempt_at=utc_now(), last_error=None)

async def list_batches(db, app_id, offset=0):
    def count(*statuses):
        return func.count().filter(Job.status.in_(statuses))
    rows = (await db.execute(select(Job.batch_id, LandlordImportFile.filename, func.min(Job.created_at).label('created_at'),
        func.count().label('total'), count('queued', 'processing').label('pending'), count('retrying').label('retrying'),
        count('sent').label('added'), count('skipped').label('skipped'), count('failed').label('failed'))
        .outerjoin(LandlordImportFile, and_(LandlordImportFile.batch_id == Job.batch_id, LandlordImportFile.app_id == app_id))
        .where(Job.app_id == app_id).group_by(Job.batch_id, LandlordImportFile.filename)
        .order_by(func.min(Job.created_at).desc(), Job.batch_id).offset(offset).limit(BATCH_PAGE_SIZE))).all()
    return [dict(batch_id=str(r.batch_id), filename=r.filename, created_at=r.created_at, total=r.total,
        completed=r.added + r.skipped + r.failed, added=r.added, skipped=r.skipped, failed=r.failed, retrying=r.retrying,
        status='processing' if r.pending or r.retrying else 'needs_attention' if r.failed else 'completed') for r in rows]

async def retry_batch(db, app_id, batch_id):
    requeued = (await db.execute(update(Job).where(Job.app_id == app_id, Job.batch_id == batch_id,
        Job.status.in_(RESUMABLE)).values(**_requeued()).returning(Job.id))).scalars().all()
    if not requeued:
        known = await db.scalar(select(Job.id).where(Job.app_id == app_id, Job.batch_id == batch_id).limit(1))
        raise HTTPException(409, 'Nothing left to retry in this import') if known else HTTPException(404, 'Import not found')
    await db.commit()
    return {'requeued': len(requeued)}

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
        job.last_error = 'We could not finish this row. Please retry.'
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
            existing = (await db.execute(select(User).where(User.email == job.email, User.app_id == job.app_id))).scalar_one_or_none()
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

            from app.models.token import RegistrationToken
            from app.services.tokens import generate_registration_code
            from app.core.redis import cache_registration_token
            tok_code = generate_registration_code()
            token = RegistrationToken(
                code=tok_code,
                landlord_id=user.id,
                house_number=user.house_number,
                street_name=user.street_name,
                relationship_type="family",
                expires_at=utc_now() + timedelta(days=7),
                app_id=job.app_id
            )
            db.add(token)
            await db.flush()
            try:
                await cache_registration_token(
                    app_id=job.app_id,
                    code=tok_code,
                    token_data={
                        "id": str(token.id),
                        "code": tok_code,
                        "landlord_id": str(user.id),
                        "app_id": job.app_id,
                        "house_number": user.house_number,
                        "street_name": user.street_name
                    },
                    ttl_seconds=1200
                )
            except Exception:
                pass

            # Persist the account, token and encrypted credential before sending. A crash
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
        job.last_error = ('Welcome emails are not set up yet. Contact Residence support; your rows are saved and can be retried.'
            if not notification_service.api_key else 'We could not send this welcome email. Check the email address, then retry.')
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
    if settings.ENVIRONMENT.lower() == 'production' and not notification_service.api_key:
        logger.critical('RESEND_API_KEY is not set: landlord welcome emails cannot be sent')
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
