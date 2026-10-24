from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_db
from app.schemas.landlord import LandlordImportSchema, LandlordImportResponseSchema
from app.services import landlords as landlord_service
from app.dependencies import check_roles, validate_tenant
from app.models.user import UserRole
import uuid

router = APIRouter()

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
        raise HTTPException(422, "Submit 1 to 500 landlords per batch")
    return await landlord_service.import_landlords(db, import_data, app_id)


@router.get('/')
async def resident_directory(
    app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER])),
):
    from sqlalchemy import select
    from app.models.user import User
    from app.models.token import RegistrationToken
    from app.models.base import utc_now
    users = (await db.execute(select(User).where(User.app_id == app_id).order_by(User.full_name, User.id))).scalars().all()
    tokens = (await db.execute(select(RegistrationToken).where(
        RegistrationToken.app_id == app_id, RegistrationToken.is_used.is_(False), RegistrationToken.is_revoked.is_(False),
        RegistrationToken.expires_at > utc_now()
    ).order_by(RegistrationToken.created_at.desc()))).scalars().all()
    latest = {}
    for token in tokens:
        latest.setdefault(token.landlord_id, token.code)
    return [{
        'id': user.id, 'full_name': user.full_name or user.email, 'email': user.email,
        'phone': user.phone, 'must_change_password': user.must_change_password, 'landlord_id': user.landlord_id,
        'house_number': user.house_number, 'street_name': user.street_name,
        'registration_token': latest.get(user.id, ''),
        'status': 'suspended' if user.is_revoked else 'active',
        'type': 'permanent' if UserRole.LANDLORD in user.roles else 'tenant',
        'occupants': [{'id': occupant.id, 'full_name': occupant.full_name or occupant.email,
            'email': occupant.email, 'phone': occupant.phone, 'landlord_id': occupant.landlord_id,
            'invitation_id': occupant.invitation_id, 'relationship_type': occupant.relationship_type, 'roles': occupant.roles, 'is_revoked': occupant.is_revoked}
            for occupant in users if occupant.landlord_id == user.id],
    } for user in users if (UserRole.LANDLORD in user.roles or UserRole.RESIDENT in user.roles) and user.landlord_id is None]

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
    return await landlord_service.import_landlords(db, entries, app_id, batch_id=batch_id)

@router.get('/onboarding-jobs')
async def onboarding_jobs(app_id: str = Depends(validate_tenant), db: AsyncSession = Depends(get_db),
    current_user = Depends(check_roles([UserRole.CARETAKER])), offset: int = 0):
    from sqlalchemy import select
    from app.models.onboarding import LandlordOnboardingJob as Job
    if offset < 0:
        raise HTTPException(422, 'Invalid page offset')
    jobs = (await db.execute(select(Job).where(Job.app_id == app_id).order_by(Job.created_at.desc(), Job.id).offset(offset).limit(100))).scalars().all()
    return [dict(id=str(j.id), batch_id=str(j.batch_id), email=j.email, full_name=j.full_name,
        phone=j.phone, house_number=j.house_number, status=j.status, attempts=j.attempts,
        last_error=j.last_error, next_attempt_at=j.next_attempt_at, created_at=j.created_at) for j in jobs]

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
