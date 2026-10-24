import asyncio
import csv
import io
import logging
import uuid
from datetime import timedelta
from sqlalchemy import select, delete, tuple_, or_, text
from app.models.base import utc_now
from app.models.history import HistoryJob, HistoryExportChunk
from app.models.access_log import AccessLog
from app.models.admin_audit import AdminAuditLog
from app.models.estate import Estate
from app.services.history_policy import retention, BATCH_SIZE

logger=logging.getLogger(__name__)
HISTORY_LOCK_KEY=742190285
FIELDS=['id','created_at','app_id','event_type','identifier','status','visitor_name','denial_reason','details','user_id','resident_id']

def csv_cell(value):
    text='' if value is None else str(value)
    # Neutralize spreadsheet formulas, including whitespace-prefixed formulas.
    return "'"+text if text.lstrip().startswith(('=','+','-','@','\t','\r')) else text

def audit(db, job, action, **changes):
    db.add(AdminAuditLog(actor_id=job.actor_id,actor_email=job.actor_email,action=action,
        target_type='estate_history',target_id=str(job.id),changes={'app_id':job.app_id,**changes}))

def selection(job):
    filters=[AccessLog.app_id==job.app_id,AccessLog.created_at>=job.start_at,
        AccessLog.created_at<job.end_at,AccessLog.created_at<=job.snapshot_at]
    if job.status_filter: filters.append(AccessLog.status==job.status_filter)
    return filters

async def create_job(db,user,kind,start,end,status_filter=None):
    from fastapi import HTTPException
    now=utc_now();days,_=await retention(db,user.app_id)
    if start is None: start=now-timedelta(days=days) if kind=='export' else now-timedelta(days=36500)
    if end is None: end=now
    end=min(end,now)
    if kind=='export': start=max(start,now-timedelta(days=days))
    if start>=end: raise HTTPException(422,'Choose a valid date range within the available history')
    # Serialize enqueueing with purge confirmation and export invalidation.
    await db.execute(select(Estate.id).where(Estate.app_id==user.app_id).with_for_update())
    pending=await db.scalar(select(HistoryJob.id).where(HistoryJob.app_id==user.app_id,
        HistoryJob.status.in_(['queued','processing','previewing'])).limit(1))
    if pending: raise HTTPException(409,'Wait for the current history job to finish')
    job=HistoryJob(app_id=user.app_id,kind=kind,status='queued' if kind=='export' else 'previewing',
        actor_id=user.id,actor_email=user.email,start_at=start,end_at=end,snapshot_at=now,
        status_filter=status_filter,retention_days=days,preview_count=0,processed_count=0,attempts=0,
        next_attempt_at=now,expires_at=now+timedelta(hours=24) if kind=='export' else None)
    db.add(job);await db.flush()
    audit(db,job,'history.export.request' if kind=='export' else 'history.purge.preview',start=start.isoformat(),end=end.isoformat())
    await db.commit();await db.refresh(job)
    return job

async def confirm_purge(db,user,job_id,confirmation):
    from fastapi import HTTPException
    await db.execute(select(Estate.id).where(Estate.app_id==user.app_id).with_for_update())
    job=(await db.execute(select(HistoryJob).where(HistoryJob.id==job_id,HistoryJob.app_id==user.app_id).with_for_update())).scalar_one_or_none()
    if not job: raise HTTPException(404,'History job not found')
    if job.kind!='purge' or job.status!='awaiting_confirmation' or job.actor_id!=user.id:
        raise HTTPException(409,'Create your own completed purge preview first')
    if confirmation!='PURGE' or not job.expires_at or job.expires_at<=utc_now():
        raise HTTPException(422,'Type PURGE to confirm a preview less than 15 minutes old')
    active=await db.scalar(select(HistoryJob.id).where(HistoryJob.app_id==user.app_id,
        HistoryJob.status.in_(['queued','processing','previewing'])).limit(1))
    if active: raise HTTPException(409,'Wait for the current history job to finish')
    # Invalidate all old exports before deleting source records. Their chunks are
    # removed separately in bounded transactions, but cannot be downloaded now.
    from sqlalchemy import update
    await db.execute(update(HistoryJob).where(HistoryJob.app_id==user.app_id,HistoryJob.kind=='export')
        .values(expires_at=utc_now()).execution_options(synchronize_session=False))
    job.status='queued';job.confirmed_at=utc_now();job.expires_at=None
    job.cursor_at=None;job.cursor_id=None;job.next_attempt_at=utc_now()
    audit(db,job,'history.purge.confirm',preview_count=job.preview_count,start=job.start_at.isoformat(),end=job.end_at.isoformat())
    await db.commit();return job

async def process_one(db,size=BATCH_SIZE):
    now=utc_now()
    # One history chunk globally at a time keeps exports/purges from flooding
    # the primary. This transaction-scoped lease is released on commit/crash.
    if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),{'key':HISTORY_LOCK_KEY}):
        await db.rollback();return False
    job=(await db.execute(select(HistoryJob).where(HistoryJob.status.in_(['queued','processing','previewing']),
        or_(HistoryJob.next_attempt_at.is_(None),HistoryJob.next_attempt_at<=now))
        .order_by(HistoryJob.updated_at,HistoryJob.created_at,HistoryJob.id).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
    if not job: await db.rollback();return False
    job_id=job.id
    original_update=job.updated_at
    try:
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        # One short transaction per chunk. A crash rolls back both the chunk and
        # cursor/progress; another consumer resumes the same database job.
        estate=await db.scalar(select(Estate.id).where(Estate.app_id==job.app_id).with_for_update(skip_locked=True))
        if not estate: await db.rollback();return False
        if job.kind=='export':
            days,_=await retention(db,job.app_id)
            if job.expires_at<=now or days<job.retention_days:
                job.status='expired';await db.commit();return True
        preview=job.status=='previewing'
        job.status='previewing' if preview else 'processing'
        conditions=selection(job)
        if job.cursor_at is not None and job.cursor_id is not None:
            conditions.append(tuple_(AccessLog.created_at,AccessLog.id)>(job.cursor_at,job.cursor_id))
        rows=(await db.execute(select(AccessLog).where(*conditions).order_by(AccessLog.created_at,AccessLog.id).limit(size))).scalars().all()
        if preview:
            job.preview_count+=len(rows)
        elif job.kind=='export':
            stream=io.StringIO(newline='');writer=csv.writer(stream)
            if job.processed_count==0: writer.writerow(FIELDS)
            for row in rows: writer.writerow([csv_cell(getattr(row,field)) for field in FIELDS])
            if rows or job.processed_count==0:
                db.add(HistoryExportChunk(job_id=job.id,sequence=job.processed_count,content=stream.getvalue().encode('utf-8-sig' if job.processed_count==0 else 'utf-8')))
            job.processed_count+=len(rows)
        else:
            if rows:
                count=(await db.execute(delete(AccessLog).where(AccessLog.id.in_([row.id for row in rows]))
                    .execution_options(synchronize_session=False))).rowcount
                job.processed_count+=count
        if rows: job.cursor_at=rows[-1].created_at;job.cursor_id=rows[-1].id
        if len(rows)<size:
            job.status='awaiting_confirmation' if preview else 'completed'
            if preview: job.expires_at=utc_now()+timedelta(minutes=15)
            else: audit(db,job,'history.'+job.kind+'.complete',processed_count=job.processed_count)
        job.attempts=0;job.last_error=None
        await db.commit()
    except Exception:
        await db.rollback()
        job=(await db.execute(select(HistoryJob).where(HistoryJob.id==job_id).with_for_update())).scalar_one()
        if job.updated_at!=original_update or job.status not in ['queued','processing','previewing']:
            # Another consumer already advanced/recovered the job after rollback.
            await db.commit();return True
        job.attempts+=1
        job.last_error='History processing failed; retry after checking database availability'
        job.next_attempt_at=utc_now()+timedelta(seconds=min(300,2**job.attempts))
        if job.attempts>=5:
            job.status='failed';audit(db,job,'history.'+job.kind+'.failed',processed_count=job.processed_count)
        await db.commit()
        logger.warning('History job %s failed',job_id)
    return True

async def cleanup_exports(db,size=100):
    # Expired and invalidated artifacts are inaccessible immediately; reclaim
    # their blobs incrementally so a large purge cannot monopolize storage IO.
    expired=select(HistoryJob.id).where(HistoryJob.kind=='export',HistoryJob.expires_at<=utc_now())
    keys=select(HistoryExportChunk.job_id,HistoryExportChunk.sequence).where(HistoryExportChunk.job_id.in_(expired)).limit(size)
    count=(await db.execute(delete(HistoryExportChunk).where(tuple_(HistoryExportChunk.job_id,HistoryExportChunk.sequence).in_(keys)))).rowcount
    await db.commit();return count

_task=None
async def _run():
    from app.core.database import SessionLocal
    while True:
        try:
            async with SessionLocal() as db:
                worked=await process_one(db)
                await cleanup_exports(db,size=10)
            await asyncio.sleep(0.05 if worked else 3)
        except asyncio.CancelledError: raise
        except Exception:
            logger.exception('History worker unavailable');await asyncio.sleep(5)

def start_worker():
    global _task
    _task=asyncio.create_task(_run())

async def stop_worker():
    if _task:
        _task.cancel()
        try: await _task
        except asyncio.CancelledError: pass
