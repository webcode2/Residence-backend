from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.landlord import LandlordImportSchema, LandlordImportResponseSchema
from app.services import landlords as landlord_service
from app.services.worker_wake import wake_worker
from app.dependencies import check_roles, validate_tenant
from app.models.user import UserRole
import logging
import uuid

logger = logging.getLogger(__name__)

router = APIRouter()


async def _wake_email_worker():
    """Welcome emails are sent by the worker; the request only queues and wakes it."""
    await wake_worker("welcome_email")


QUEUED_EMAIL = {"email_sent": None, "email_status": "queued", "email_warning": None}

@router.post("/import", response_model=List[LandlordImportResponseSchema])
async def import_landlords(
    import_data: List[LandlordImportSchema],
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Bulk import landlords.
    Only accessible by Caretakers (Estate Admins).
    """
    if not 1 <= len(import_data) <= 500:
        raise HTTPException(422, "Submit 1 to 500 hosts / property owners per batch")
    results = await landlord_service.import_landlords(db, import_data, app_id)
    await _wake_email_worker()
    return results


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_landlord(
    data: LandlordImportSchema,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Directly create a single landlord in the estate directory without queueing.
    Immediately creates the user account and active registration token.
    """
    from sqlalchemy import select
    from app.models.user import User, UserRole
    from app.models.token import RegistrationToken
    from app.models.onboarding import LandlordOnboardingJob
    from app.models.base import utc_now
    from app.services.auth import get_password_hash_async
    from app.services.tokens import generate_registration_code
    from app.services.landlords import cipher
    from app.core.redis import get_redis_client
    from datetime import datetime, timedelta, UTC
    import secrets
    import json

    clean_email = str(data.email).strip().lower()
    existing = (await db.execute(select(User.id).where(User.email == clean_email, User.app_id == app_id))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email already belongs to someone in your estate directory."
        )

    temp_password = secrets.token_urlsafe(14)
    user = User(
        email=clean_email,
        full_name=data.name.strip(),
        phone=data.phone.strip(),
        house_number=data.house_number.strip(),
        street_name=data.street_name.strip() if data.street_name else None,
        app_id=app_id,
        roles=[UserRole.LANDLORD],
        must_change_password=True,
        hashed_password=await get_password_hash_async(temp_password)
    )
    db.add(user)
    await db.flush()

    token = RegistrationToken(
        code=generate_registration_code(),
        landlord_id=user.id,
        house_number=user.house_number,
        street_name=user.street_name,
        relationship_type="family",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7),
        app_id=app_id
    )
    db.add(token)
    await db.commit()
    await db.refresh(user)

    from app.core.redis import cache_registration_token
    try:
        await cache_registration_token(
            app_id=app_id,
            code=token.code,
            token_data={
                "id": str(token.id),
                "code": token.code,
                "landlord_id": str(token.landlord_id),
                "app_id": app_id,
                "house_number": token.house_number,
                "street_name": token.street_name
            },
            ttl_seconds=1200
        )
    except Exception:
        pass

    job_id = uuid.uuid4()
    now = utc_now()
    job = LandlordOnboardingJob(
        id=job_id,
        batch_id=uuid.uuid4(),
        app_id=app_id,
        email=clean_email,
        full_name=user.full_name,
        phone=user.phone,
        house_number=user.house_number,
        street_name=user.street_name,
        status="queued",
        attempts=0,
        next_attempt_at=now,
        password_ciphertext=cipher().encrypt(temp_password.encode()).decode(),
        user_id=user.id,
    )
    db.add(job)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        # If a job row already exists for this app/email, requeue it for the new account.
        existing_job = (await db.execute(
            select(LandlordOnboardingJob).where(
                LandlordOnboardingJob.app_id == app_id,
                LandlordOnboardingJob.email == clean_email,
            )
        )).scalar_one_or_none()
        if existing_job is None:
            raise
        existing_job.batch_id = job.batch_id
        existing_job.user_id = user.id
        existing_job.full_name = user.full_name
        existing_job.phone = user.phone
        existing_job.house_number = user.house_number
        existing_job.street_name = user.street_name
        existing_job.status = "queued"
        existing_job.attempts = 0
        existing_job.next_attempt_at = now
        existing_job.last_error = None
        existing_job.lease_until = None
        existing_job.password_ciphertext = cipher().encrypt(temp_password.encode()).decode()
        await db.commit()
        job_id = existing_job.id

    # Register follow-up status in Redis for fast polling/UI updates.
    try:
        redis_client = get_redis_client()
        if redis_client:
            payload = json.dumps({
                "job_id": str(job_id),
                "user_id": str(user.id),
                "app_id": app_id,
                "email": clean_email,
                "status": "queued",
                "updated_at": now.isoformat(),
            })
            await redis_client.setex(f"landlord:onboarding:{job_id}", 7 * 24 * 3600, payload)
    except Exception:
        logger.warning("Failed to cache landlord onboarding status in Redis: job_id=%s", job_id, exc_info=True)

    from app.core.redis import invalidate_cached_user
    await invalidate_cached_user(app_id, user.email)  # never serve a stale profile for a re-used email
    await _wake_email_worker()
    return {
        "id": str(user.id),
        "full_name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "house_number": user.house_number,
        "street_name": user.street_name,
        "registration_token": token.code,
        "status": "active",
        "type": "permanent",
        "temporary_password": temp_password,
        "job_id": str(job_id),
        **QUEUED_EMAIL,
        "message": "Host / Property Owner added successfully. Welcome email is on its way.",
    }


@router.get('/')
async def resident_directory(
    app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER])),
):
    from sqlalchemy import select
    from app.models.user import User, UserRole
    from app.models.token import RegistrationToken
    from app.models.base import utc_now
    from datetime import datetime, timedelta, UTC
    from app.services.tokens import generate_registration_code
    from app.core.redis import cache_registration_token

    users = (await db.execute(select(User).where(User.app_id == app_id, User.archived_at.is_(None)).order_by(User.full_name, User.id))).scalars().all()
    tokens = (await db.execute(select(RegistrationToken).where(
        RegistrationToken.app_id == app_id, RegistrationToken.is_used.is_(False), RegistrationToken.is_revoked.is_(False),
        RegistrationToken.expires_at > utc_now()
    ).order_by(RegistrationToken.created_at.desc()))).scalars().all()
    latest = {}
    for token in tokens:
        latest.setdefault(token.landlord_id, token.code)

    # Ensure every landlord in the directory has an active registration token
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    new_tokens = []
    landlords = [u for u in users if (UserRole.LANDLORD in u.roles) and u.landlord_id is None]
    for lld in landlords:
        if lld.id not in latest:
            code = generate_registration_code()
            new_tok = RegistrationToken(
                code=code,
                landlord_id=lld.id,
                house_number=lld.house_number,
                street_name=lld.street_name,
                relationship_type="family",
                expires_at=now_utc + timedelta(days=7),
                app_id=app_id
            )
            db.add(new_tok)
            new_tokens.append(new_tok)
            latest[lld.id] = code

    if new_tokens:
        await db.commit()
        for tok in new_tokens:
            try:
                await cache_registration_token(
                    app_id=app_id,
                    code=tok.code,
                    token_data={
                        "id": str(tok.id),
                        "code": tok.code,
                        "landlord_id": str(tok.landlord_id),
                        "app_id": app_id,
                        "house_number": tok.house_number,
                        "street_name": tok.street_name
                    },
                    ttl_seconds=1200
                )
            except Exception:
                pass

    from app.models.onboarding import AccountPasswordResetJob
    reset_rows = (await db.execute(select(
        AccountPasswordResetJob.user_id, AccountPasswordResetJob.id, AccountPasswordResetJob.status,
    ).where(AccountPasswordResetJob.app_id == app_id)
     .distinct(AccountPasswordResetJob.user_id)
     .order_by(AccountPasswordResetJob.user_id, AccountPasswordResetJob.created_at.desc(),
               AccountPasswordResetJob.id.desc()))).all()
    reset_jobs = {user_id: {"id": job_id, "status": state} for user_id, job_id, state in reset_rows}
    retained_emails = {
        email.lower() for email in (await db.execute(select(User.archive_email).where(
            User.app_id == app_id, User.archive_hold_at.is_not(None),
            User.archive_email.is_not(None),
        ))).scalars()
    }
    directory_ids = {user.id for user in users}
    return [{
        'id': user.id, 'full_name': user.full_name or user.email, 'email': user.email,
        'returning_user': user.email.lower() in retained_emails,
        'password_reset': reset_jobs.get(user.id),
        'phone': user.phone, 'must_change_password': user.must_change_password, 'landlord_id': user.landlord_id,
        'house_number': user.house_number, 'street_name': user.street_name,
        'registration_token': latest.get(user.id, ''),
        'status': 'suspended' if user.is_revoked else 'active',
        'type': 'permanent' if UserRole.LANDLORD in user.roles else 'tenant',
        'occupants': [{'id': occupant.id, 'full_name': occupant.full_name or occupant.email,
            'email': occupant.email, 'phone': occupant.phone, 'landlord_id': occupant.landlord_id,
            'returning_user': occupant.email.lower() in retained_emails,
            'password_reset': reset_jobs.get(occupant.id),
            'invitation_id': occupant.invitation_id, 'relationship_type': occupant.relationship_type, 'roles': occupant.roles, 'is_revoked': occupant.is_revoked}
            for occupant in users if occupant.landlord_id == user.id],
    } for user in users if (UserRole.LANDLORD in user.roles or UserRole.RESIDENT in user.roles) and (user.landlord_id is None or user.landlord_id not in directory_ids)]

@router.post('/import-file', response_model=List[LandlordImportResponseSchema])
async def import_landlord_file(file: UploadFile = File(...), app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db), current_user = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services.landlord_files import parse_file, MAX_BYTES
    from starlette.concurrency import run_in_threadpool
    content = await file.read(MAX_BYTES + 1)
    entries = await run_in_threadpool(parse_file, file.filename, content)
    from app.models.onboarding import LandlordImportFile
    from pathlib import PurePosixPath
    filename = PurePosixPath((file.filename or 'landlords.csv').replace('\\','/')).name
    suffix = filename.rsplit('.',1)[-1].lower()
    filename = ''.join(c for c in filename if ord(c) >= 32 and ord(c) != 127) or 'landlords.csv'
    if len(filename) > 255:
        filename = filename[:254-len(suffix)] + '.' + suffix
    types = {'csv':'text/csv', 'xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xls':'application/vnd.ms-excel'}
    batch_id = uuid.uuid4()
    db.add(LandlordImportFile(app_id=app_id, filename=filename, content_type=types[suffix],
        content=content, size_bytes=len(content), batch_id=batch_id, uploaded_by=current_user.id))
    results = await landlord_service.import_landlords(db, entries, app_id, batch_id=batch_id)
    await _wake_email_worker()
    return results

@router.get('/onboarding-jobs')
async def onboarding_jobs(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER])), offset: int = 0,
    batch_id: uuid.UUID | None = None, problems: bool = False):
    from sqlalchemy import select
    from app.models.onboarding import LandlordOnboardingJob as Job
    if offset < 0:
        raise HTTPException(422, 'Invalid page offset')
    query = select(Job).where(Job.app_id == app_id)
    if batch_id:
        query = query.where(Job.batch_id == batch_id)
    if problems:
        query = query.where(Job.status.in_(['failed', 'retrying', 'skipped']))
    # A batch holds at most one file's rows, so its problem list needs no paging.
    limit = 500 if batch_id else 100
    jobs = (await db.execute(query.order_by(Job.created_at.desc(), Job.id).offset(offset).limit(limit))).scalars().all()
    return [dict(id=str(j.id), batch_id=str(j.batch_id), email=j.email, full_name=j.full_name,
        phone=j.phone, house_number=j.house_number, status=j.status, attempts=j.attempts,
        last_error=j.last_error, next_attempt_at=j.next_attempt_at, created_at=j.created_at) for j in jobs]

@router.get('/import-batches')
async def import_batches(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER])), offset: int = 0):
    if offset < 0:
        raise HTTPException(422, 'Invalid page offset')
    return await landlord_service.list_batches(db, app_id, offset)

@router.post('/import-batches/{batch_id}/retry')
async def retry_import_batch(batch_id: uuid.UUID, app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db), current_user = Depends(check_roles([UserRole.CARETAKER]))):
    result = await landlord_service.retry_batch(db, app_id, batch_id)
    await _wake_email_worker()
    return result

@router.post('/onboarding-jobs/{job_id}/retry')
async def retry_onboarding_job(job_id: uuid.UUID, app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db), current_user = Depends(check_roles([UserRole.CARETAKER]))):
    from sqlalchemy import select
    from app.models.onboarding import LandlordOnboardingJob as Job
    from app.models.base import utc_now
    job = (await db.execute(select(Job).where(Job.id == job_id, Job.app_id == app_id).with_for_update())).scalar_one_or_none()
    if not job:
        raise HTTPException(404, 'Job not found')
    if job.status not in ['failed', 'retrying']:
        raise HTTPException(409, 'Only failed or retrying jobs can be retried')
    job.status = 'queued'
    job.attempts = 0
    job.next_attempt_at = utc_now()
    job.last_error = None
    await db.commit()
    await _wake_email_worker()
    return {'status':'queued'}

@router.get('/household')
async def landlord_household(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.LANDLORD]))):
    from sqlalchemy import select
    from app.models.user import User
    members = (await db.execute(select(User).where(User.app_id == app_id, User.landlord_id == current_user.id)
        .order_by(User.full_name, User.id))).scalars().all()
    return [dict(id=str(u.id), full_name=u.full_name, email=u.email, house_number=u.house_number,
        relationship_type=u.relationship_type, invitation_id=str(u.invitation_id) if u.invitation_id else None,
        is_revoked=u.is_revoked) for u in members]

@router.get('/import-files')
async def import_files(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER])), offset: int = 0):
    from sqlalchemy import select, func
    from sqlalchemy.orm import defer
    from app.models.onboarding import LandlordImportFile as StoredFile, LandlordOnboardingJob as Job
    if offset < 0:
        raise HTTPException(422, 'Invalid page offset')
    active = select(Job.batch_id, func.count().label('pending')).where(Job.app_id == app_id,
        Job.status.in_(['queued','processing','retrying'])).group_by(Job.batch_id).subquery()
    rows = (await db.execute(select(StoredFile, func.coalesce(active.c.pending,0))
        .options(defer(StoredFile.content)).outerjoin(active, active.c.batch_id == StoredFile.batch_id)
        .where(StoredFile.app_id == app_id, StoredFile.deleted_at.is_(None))
        .order_by(StoredFile.created_at.desc(),StoredFile.id).offset(offset).limit(50))).all()
    return [dict(id=str(f.id),filename=f.filename,size_bytes=f.size_bytes,batch_id=str(f.batch_id),
        created_at=f.created_at,pending_jobs=pending,can_delete=pending==0) for f,pending in rows]

@router.get('/import-files/{file_id}/download')
async def download_import_file(file_id: uuid.UUID, app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db), current_user = Depends(check_roles([UserRole.CARETAKER]))):
    from sqlalchemy import select
    from fastapi.responses import Response
    from urllib.parse import quote
    from app.models.onboarding import LandlordImportFile as StoredFile
    stored = (await db.execute(select(StoredFile).where(StoredFile.id == file_id,
        StoredFile.app_id == app_id,StoredFile.deleted_at.is_(None)))).scalar_one_or_none()
    if not stored:
        raise HTTPException(404,'Import file not found')
    return Response(content=stored.content,media_type=stored.content_type,headers={
        'Content-Disposition': "attachment; filename*=UTF-8''"+quote(stored.filename,safe=''),
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@router.delete('/import-files/{file_id}', status_code=204)
async def delete_import_file(file_id: uuid.UUID, app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db), current_user = Depends(check_roles([UserRole.CARETAKER]))):
    from sqlalchemy import select
    from app.models.base import utc_now
    from app.models.onboarding import LandlordImportFile as StoredFile, LandlordOnboardingJob as Job
    stored = (await db.execute(select(StoredFile).where(StoredFile.id == file_id,
        StoredFile.app_id == app_id,StoredFile.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if not stored:
        raise HTTPException(404,'Import file not found')
    active = await db.scalar(select(Job.id).where(Job.app_id == app_id,Job.batch_id == stored.batch_id,
        Job.status.in_(['queued','processing','retrying'])).limit(1))
    if active:
        raise HTTPException(409,'Wait until this import finishes before deleting its file')
    stored.content = None
    stored.deleted_at = utc_now()
    await db.commit()


@router.post('/{user_id}/resend-welcome')
async def resend_landlord_welcome(
    user_id: uuid.UUID,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER]))
):
    """Generate a new temporary password and queue a welcome email for an existing landlord."""
    from sqlalchemy import select
    from app.models.user import User, UserRole
    from app.models.onboarding import LandlordOnboardingJob
    from app.models.base import utc_now
    from app.services.auth import get_password_hash_async
    from app.services.landlords import cipher
    from app.core.redis import get_redis_client
    import secrets
    import json

    user = (await db.execute(
        select(User).where(User.id == user_id, User.app_id == app_id)
    )).scalar_one_or_none()
    if not user or UserRole.LANDLORD not in user.roles:
        raise HTTPException(status_code=404, detail="Host / Property Owner not found")
    if user.is_revoked:
        raise HTTPException(status_code=409, detail="Cannot resend welcome email for a revoked host / property owner")

    temp_password = secrets.token_urlsafe(14)
    hashed_password = await get_password_hash_async(temp_password)
    user.hashed_password = hashed_password
    user.must_change_password = True
    user_id, user_email = user.id, user.email

    job_id = uuid.uuid4()
    now = utc_now()
    job = LandlordOnboardingJob(
        id=job_id,
        batch_id=uuid.uuid4(),
        app_id=app_id,
        email=user.email,
        full_name=user.full_name or user.email,
        phone=user.phone,
        house_number=user.house_number,
        street_name=user.street_name,
        status="queued",
        attempts=0,
        next_attempt_at=now,
        password_ciphertext=cipher().encrypt(temp_password.encode()).decode(),
        user_id=user.id,
    )
    db.add(job)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        # The rollback also discarded the new password; re-apply it, or the emailed
        # temporary password would not work.
        user = await db.get(User, user_id, populate_existing=True)
        user.hashed_password = hashed_password
        user.must_change_password = True
        existing_job = (await db.execute(
            select(LandlordOnboardingJob).where(
                LandlordOnboardingJob.app_id == app_id,
                LandlordOnboardingJob.email == user_email,
            )
        )).scalar_one_or_none()
        if existing_job is None:
            raise
        existing_job.batch_id = job.batch_id
        existing_job.user_id = user.id
        existing_job.full_name = user.full_name or user.email
        existing_job.phone = user.phone
        existing_job.house_number = user.house_number
        existing_job.street_name = user.street_name
        existing_job.status = "queued"
        existing_job.attempts = 0
        existing_job.next_attempt_at = now
        existing_job.last_error = None
        existing_job.lease_until = None
        existing_job.password_ciphertext = cipher().encrypt(temp_password.encode()).decode()
        await db.commit()
        job_id = existing_job.id

    from app.core.redis import invalidate_cached_user
    await invalidate_cached_user(app_id, user_email)  # password + must_change_password changed

    try:
        redis_client = get_redis_client()
        if redis_client:
            payload = json.dumps({
                "job_id": str(job_id),
                "user_id": str(user.id),
                "app_id": app_id,
                "email": user.email,
                "status": "queued",
                "updated_at": now.isoformat(),
            })
            await redis_client.setex(f"landlord:onboarding:{job_id}", 7 * 24 * 3600, payload)
    except Exception:
        logger.warning("Failed to cache landlord onboarding status in Redis: job_id=%s", job_id, exc_info=True)

    await _wake_email_worker()
    return {
        "user_id": str(user.id),
        "job_id": str(job_id),
        **QUEUED_EMAIL,
        "temporary_password": temp_password,
        "message": "Welcome email is on its way. A new temporary password was generated.",
    }
