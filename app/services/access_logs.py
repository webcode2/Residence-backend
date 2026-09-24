from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
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
        return

    if db is None:
        logger.warning("Access log dropped: Redis unavailable and no DB session provided")
        return

    db.add(AccessLog(
        app_id=app_id,
        event_type=event_type,
        identifier=identifier,
        status=status,
        denial_reason=denial_reason,
        user_id=_parse_uuid(user_id),
        resident_id=_parse_uuid(resident_id),
        visitor_name=visitor_name,
        details=details,
    ))
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

        db.add_all(rows)
        await db.commit()
        logger.debug(f"Drained {len(rows)} access logs from Redis queue")
        return len(rows)
    except Exception:
        await db.rollback()
        await requeue_access_logs(raw_items)
        raise


async def get_access_logs(
    db: AsyncSession,
    app_id: str,
    limit: int = 50,
    offset: int = 0,
    event_type: Optional[str] = None,
    status: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve access logs for an estate, strictly bounded by the estate's
    plan tier retention policy.
    """
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()

    retention_days = 30
    tier_name = "starter"
    if estate and estate.subscription:
        retention_days = estate.subscription.log_retention_days
        tier_name = estate.subscription.tier

    cutoff_datetime = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)

    query = (
        select(AccessLog)
        .where(
            AccessLog.app_id == app_id,
            AccessLog.created_at >= cutoff_datetime
        )
    )

    if event_type:
        query = query.where(AccessLog.event_type == event_type)
    if status:
        query = query.where(AccessLog.status == status)

    query = query.order_by(AccessLog.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    logs = list(result.scalars().all())

    return {
        "tier": tier_name,
        "log_retention_days": retention_days,
        "earliest_accessible_date": cutoff_datetime,
        "total_returned": len(logs),
        "logs": logs
    }


async def cleanup_expired_logs(db: AsyncSession, app_id: str) -> int:
    """
    Prune access logs that exceed the estate's plan tier retention period.
    """
    estate_result = await db.execute(
        select(Estate).where(Estate.app_id == app_id).options(selectinload(Estate.subscription))
    )
    estate = estate_result.scalar_one_or_none()
    if not estate or not estate.subscription:
        return 0

    retention_days = estate.subscription.log_retention_days
    cutoff_datetime = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)

    result = await db.execute(
        delete(AccessLog).where(
            AccessLog.app_id == app_id,
            AccessLog.created_at < cutoff_datetime
        )
    )
    await db.commit()
    return result.rowcount
