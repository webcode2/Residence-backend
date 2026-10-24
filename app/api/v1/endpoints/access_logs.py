from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from app.core.database import get_db
from app.schemas.access_log import AccessLogHistoryResponseSchema, HistoryJobRequest, PurgeConfirmation
from datetime import datetime, timezone
import uuid
from app.services import access_logs as log_service
from app.dependencies import validate_tenant, check_roles
from app.models.user import User, UserRole

router = APIRouter()

@router.get("/", response_model=AccessLogHistoryResponseSchema)
async def get_estate_access_logs(
    limit: int = Query(50, ge=1, le=200, description="Max logs to return"),
    offset: int = Query(0, ge=0, le=1000, description="Offset for pagination"),
    event_type: Optional[str] = Query(None, description="Filter by event type (e.g. rfid_verification, visitor_token_verification)"),
    log_status: Optional[str] = Query(None, alias="status", description="Filter by status (authorized, denied)"),
    cursor: Optional[str] = Query(None,max_length=4096),
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER, UserRole.SECURITY]))
):
    """
    Query access verification history for the current estate.
    Results are strictly bounded by the estate's plan tier log retention period
    (Starter: 30 days, Standard: 90 days, Premium: 180 days, Enterprise: 365 days).
    """
    result = await log_service.get_access_logs(
        db=db,
        app_id=app_id,
        limit=limit,
        offset=offset,
        event_type=event_type,
        status=log_status, cursor=cursor,
        start_at=start_at.astimezone(timezone.utc).replace(tzinfo=None) if start_at and start_at.tzinfo else start_at,
        end_at=end_at.astimezone(timezone.utc).replace(tzinfo=None) if end_at and end_at.tzinfo else end_at
    )
    return result

@router.post("/cleanup", response_model=dict)
async def cleanup_expired_access_logs(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Prune logs older than the estate's active tier retention limit.
    Only accessible by Caretakers (Estate Admins).
    """
    pruned_count = await log_service.cleanup_expired_logs(db, app_id, actor=current_user)
    return {
        "status": "success",
        "message": f"Pruned {pruned_count} log entries exceeding your tier's retention policy.",
        "pruned_count": pruned_count
    }

def job_view(job,retention_days=None):
    from app.models.base import utc_now
    expired=job.kind=='export' and (not job.expires_at or job.expires_at<=utc_now() or (retention_days is not None and retention_days<job.retention_days))
    view_status='expired' if expired else 'preview_expired' if job.status=='awaiting_confirmation' and job.expires_at<=utc_now() else job.status
    return dict(id=str(job.id),kind=job.kind,status=view_status,actor_email=job.actor_email,
        start_at=job.start_at,end_at=job.end_at,preview_count=job.preview_count,
        processed_count=job.processed_count,last_error=job.last_error,created_at=job.created_at,
        expires_at=job.expires_at,confirmed_at=job.confirmed_at,can_download=job.kind=='export' and job.status=='completed'
        and not expired)

@router.get('/jobs')
async def list_history_jobs(app_id: str = Depends(validate_tenant),db: AsyncSession = Depends(get_db),
    user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from sqlalchemy import select
    from app.models.history import HistoryJob
    rows=(await db.execute(select(HistoryJob).where(HistoryJob.app_id==app_id)
        .order_by(HistoryJob.created_at.desc(),HistoryJob.id).limit(50))).scalars().all()
    from app.services.history_policy import retention
    days,_=await retention(db,app_id)
    return [job_view(row,days) for row in rows]

@router.post('/exports',status_code=202)
async def queue_export(body: HistoryJobRequest,db: AsyncSession = Depends(get_db),user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services.history_jobs import create_job
    return job_view(await create_job(db,user,'export',body.start_at,body.end_at,body.status))

@router.post('/purge-preview',status_code=202)
async def queue_purge_preview(body: HistoryJobRequest,db: AsyncSession = Depends(get_db),user: User = Depends(check_roles([UserRole.CARETAKER]))):
    if body.end_at is None:
        raise HTTPException(422,'Choose the date before which records should be deleted')
    from app.services.history_jobs import create_job
    return job_view(await create_job(db,user,'purge',body.start_at,body.end_at,body.status))

@router.post('/jobs/{job_id}/confirm')
async def confirm_history_purge(job_id: uuid.UUID,body: PurgeConfirmation,db: AsyncSession = Depends(get_db),user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from app.services.history_jobs import confirm_purge
    return job_view(await confirm_purge(db,user,job_id,body.confirmation))

@router.post('/jobs/{job_id}/retry')
async def retry_history_job(job_id: uuid.UUID,db: AsyncSession = Depends(get_db),user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from sqlalchemy import select
    from app.models.history import HistoryJob
    from app.models.estate import Estate
    from app.models.base import utc_now
    await db.execute(select(Estate.id).where(Estate.app_id==user.app_id).with_for_update())
    active=await db.scalar(select(HistoryJob.id).where(HistoryJob.app_id==user.app_id,
        HistoryJob.status.in_(['queued','processing','previewing'])).limit(1))
    if active: raise HTTPException(409,'Wait for the current history job to finish')
    job=(await db.execute(select(HistoryJob).where(HistoryJob.id==job_id,HistoryJob.app_id==user.app_id).with_for_update())).scalar_one_or_none()
    if not job: raise HTTPException(404,'History job not found')
    if job.status!='failed': raise HTTPException(409,'Only failed history jobs can be retried')
    if job.kind=='export' and job.expires_at<=utc_now(): raise HTTPException(409,'Export expired; create a new export')
    job.status='previewing' if job.kind=='purge' and job.confirmed_at is None else 'queued'
    job.attempts=0;job.last_error=None;job.next_attempt_at=utc_now()
    from app.models.admin_audit import AdminAuditLog
    db.add(AdminAuditLog(actor_id=user.id,actor_email=user.email,action='history.job.retry',target_type='estate_history',target_id=str(job.id),changes={'app_id':user.app_id,'kind':job.kind}))
    await db.commit();return job_view(job)

@router.get('/jobs/{job_id}/download')
async def download_history_export(job_id: uuid.UUID,db: AsyncSession = Depends(get_db),user: User = Depends(check_roles([UserRole.CARETAKER]))):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from fastapi.responses import StreamingResponse
    from app.models.history import HistoryJob,HistoryExportChunk
    from app.models.base import utc_now
    from app.services.history_policy import retention
    job=(await db.execute(select(HistoryJob).where(HistoryJob.id==job_id,HistoryJob.app_id==user.app_id))).scalar_one_or_none()
    if not job: raise HTTPException(404,'History export not found')
    days,_=await retention(db,user.app_id)
    if job.kind!='export' or job.status!='completed' or not job.expires_at or job.expires_at<=utc_now() or days<job.retention_days:
        raise HTTPException(409,'Export is not ready or has expired; create a new export')
    factory=async_sessionmaker(db.bind,expire_on_commit=False)
    await db.commit()
    async def chunks():
        sequence=-1
        while True:
            async with factory() as session:
                valid=await session.scalar(select(HistoryJob.id).where(HistoryJob.id==job_id,HistoryJob.status=='completed',HistoryJob.expires_at>utc_now()))
                if not valid: return
                row=(await session.execute(select(HistoryExportChunk).where(HistoryExportChunk.job_id==job_id,
                    HistoryExportChunk.sequence>sequence).order_by(HistoryExportChunk.sequence).limit(1))).scalar_one_or_none()
                if row is None: return
                sequence=row.sequence;content=row.content
            yield content
    return StreamingResponse(chunks(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="access-history-{job_id}.csv"',
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
