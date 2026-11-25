from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, tuple_
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta, UTC
from app.models.access_log import AccessLog
from app.models.estate import Estate
# Ensure FK target tables are registered on Base.metadata for ORM flush.
from app.models import user as _user_model  # noqa: F401
from app.core.redis import enqueue_access_log, dequeue_access_logs, requeue_access_logs
from app.core.config import settings
import logging
import uuid

logger = logging.getLogger(__name__)


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def record_access_event(
    db: Optional[AsyncSession] = None,
    *,
    app_id: str,
    event_type: str,
    identifier: str,
    status: str,
    denial_reason: Optional[str] = None,
    user_id: Any = None,
    resident_id: Any = None,
    visitor_name: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    """
    Prefer Redis queue for access logs (worker batch-inserts).
    Falls back to synchronous DB write when Redis is unavailable.
    """
    payload = {
        "app_id": app_id,
        "event_type": event_type,
        "identifier": identifier,
        "status": status,
        "denial_reason": denial_reason,
        "user_id": str(user_id) if user_id else None,
        "resident_id": str(resident_id) if resident_id else None,
        "visitor_name": visitor_name,
        "details": details,
        "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
    }

    if await enqueue_access_log(payload):
        from app.services.worker_wake import wake_worker
        await wake_worker('access_log')
        return

    if db is None:
        logger.warning("Access log dropped: Redis unavailable and no DB session provided")
        return

    fallback_log=AccessLog(
        app_id=app_id,
        event_type=event_type,
        identifier=identifier,
        status=status,
        denial_reason=denial_reason,
        user_id=_parse_uuid(user_id),
        resident_id=_parse_uuid(resident_id),
        visitor_name=visitor_name,
        details=details,
    )
    db.add_all(await _persistable_logs(db,[fallback_log]))
    await db.commit()


async def drain_access_log_queue(
    db: AsyncSession,
    batch_size: Optional[int] = None,
) -> int:
    """Batch-insert queued access logs. Re-queues on DB failure (at-least-once)."""
    size = batch_size or settings.ACCESS_LOG_DRAIN_BATCH_SIZE
    dequeued = await dequeue_access_logs(size)
    if not dequeued:
        return 0

    raw_items = [raw for raw, _ in dequeued]
    try:
        rows: List[AccessLog] = []
        for _raw, item in dequeued:
            created_raw = item.get("created_at")
            created_at = None
            if created_raw:
                try:
                    created_at = datetime.fromisoformat(str(created_raw))
                except ValueError:
                    created_at = datetime.now(UTC).replace(tzinfo=None)

            log = AccessLog(
                app_id=item["app_id"],
                event_type=item["event_type"],
                identifier=item["identifier"],
                status=item["status"],
                denial_reason=item.get("denial_reason"),
                user_id=_parse_uuid(item.get("user_id")),
                resident_id=_parse_uuid(item.get("resident_id")),
                visitor_name=item.get("visitor_name"),
                details=item.get("details"),
            )
            if created_at is not None:
                log.created_at = created_at
            rows.append(log)

        await _clear_missing_people(db, rows)
        rows=await _persistable_logs(db,rows)
        db.add_all(rows)
        await db.commit()
        logger.debug(f"Drained {len(rows)} access logs from Redis queue")
        return len(rows)
    except Exception:
        await db.rollback()
        await requeue_access_logs(raw_items)
        raise


async def _clear_missing_people(db, rows):
    """Keep gate records whose resident/user was deleted after the event was queued.

    Without this, one such row fails the foreign key, the batch is re-queued, and the
    access-log queue is blocked for good. The event is kept; only the dangling link is
    cleared.
    """
    from app.models.user import User
    ids = {ref for row in rows for ref in (row.user_id, row.resident_id) if ref}
    if not ids:
        return
    existing = set((await db.execute(select(User.id).where(User.id.in_(ids)))).scalars().all())
    for row in rows:
        if row.user_id and row.user_id not in existing:
            row.user_id = None
        if row.resident_id and row.resident_id not in existing:
            row.resident_id = None


async def _persistable_logs(db,rows):
    """Do not resurrect confirmed purges when older queued events arrive late."""
    from app.models.history import HistoryJob
    if not rows: return rows
    now=datetime.now(UTC).replace(tzinfo=None)
    for row in rows:
        if row.created_at is None: row.created_at=now
        if row.created_at.tzinfo: row.created_at=row.created_at.astimezone(UTC).replace(tzinfo=None)
    apps=sorted({row.app_id for row in rows})
    # Serialize each committed insert batch with purge confirmation/deletion.
    # Verification still enqueues without this database read/lock on its Redis path.
    await db.execute(select(Estate.id).where(Estate.app_id.in_(apps)).order_by(Estate.app_id).with_for_update())
    rules=(await db.execute(select(HistoryJob).where(HistoryJob.app_id.in_(apps),HistoryJob.kind=='purge',
        HistoryJob.confirmed_at.is_not(None),HistoryJob.start_at<=max(r.created_at for r in rows),
        HistoryJob.end_at>min(r.created_at for r in rows)))).scalars().all()
    return [row for row in rows if not any(rule.app_id==row.app_id and rule.start_at<=row.created_at<rule.end_at
        and row.created_at<=rule.snapshot_at and (rule.status_filter is None or rule.status_filter==row.status) for rule in rules)]


async def get_access_logs(db,app_id,limit=50,offset=0,event_type=None,status=None,cursor=None,start_at=None,end_at=None):
    from app.services.history_policy import retention
    from app.services import history_cursor
    from fastapi import HTTPException
    days,tier=await retention(db,app_id)
    now=datetime.now(UTC).replace(tzinfo=None)
    if start_at and end_at and start_at>=end_at:
        raise HTTPException(422,'The end date must be after the start date')
    cutoff=now-timedelta(days=days)
    filters={'app_id':app_id,'status':status,'event_type':event_type,
        'start_at':start_at.isoformat() if start_at else None,'end_at':end_at.isoformat() if end_at else None}
    data=None
    if cursor:
        data=history_cursor.decode(cursor)
        if data.get('filters')!=filters: raise HTTPException(422,'History cursor does not match these filters')
        try:
            snapshot=datetime.fromisoformat(data['snapshot'])
            last_at=datetime.fromisoformat(data['at']);last_id=uuid.UUID(data['id'])
        except (KeyError,ValueError,TypeError): raise HTTPException(422,'Invalid history cursor') from None
    else: snapshot=now
    conditions=[AccessLog.app_id==app_id,AccessLog.created_at>=max(cutoff,start_at or cutoff),AccessLog.created_at<=snapshot]
    if end_at: conditions.append(AccessLog.created_at<end_at)
    if event_type: conditions.append(AccessLog.event_type==event_type)
    if status: conditions.append(AccessLog.status==status)
    if data: conditions.append(tuple_(AccessLog.created_at,AccessLog.id)<(last_at,last_id))
    rows=(await db.execute(select(AccessLog).where(*conditions).order_by(AccessLog.created_at.desc(),AccessLog.id.desc())
        .offset(offset if not cursor else 0).limit(limit+1))).scalars().all()
    logs=list(rows[:limit]);next_cursor=None
    if len(rows)>limit and logs:
        last=logs[-1]
        next_cursor=history_cursor.encode({'filters':filters,'snapshot':snapshot.isoformat(),'at':last.created_at.isoformat(),'id':str(last.id)})
    return {'tier':tier,'log_retention_days':days,'earliest_accessible_date':cutoff,
        'total_returned':len(logs),'logs':logs,'next_cursor':next_cursor,'has_more':bool(next_cursor)}

async def cleanup_expired_logs(db,app_id,batch_size=1000,actor=None):
    from app.services.history_policy import retention,delete_log_batch
    days,_=await retention(db,app_id)
    return await delete_log_batch(db,app_id,datetime.now(UTC).replace(tzinfo=None)-timedelta(days=days),batch_size,actor)
