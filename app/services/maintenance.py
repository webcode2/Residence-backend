import logging
from datetime import datetime, timedelta, date, UTC
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from sqlalchemy.orm import selectinload

from app.models.estate import Estate, Subscription
from app.models.access_log import AccessLog
from app.models.token import VisitorToken
from app.models import user as _user_model  # noqa: F401
from app.core.redis import (
    invalidate_subscription_cache,
    reset_monthly_quota_redis,
    list_active_quota_app_ids,
    get_monthly_quota_redis,
)
from app.services.access_logs import drain_access_log_queue
from app.services.token_state import drain_token_state_queue
from app.services.chat import drain_chat_message_queue

logger = logging.getLogger(__name__)

async def purge_expired_access_logs(db: AsyncSession) -> int:
    """Bounded per-estate cleanup; the read cutoff remains immediate."""
    from app.services.history_policy import retention,delete_log_batch
    app_ids=list((await db.execute(select(Estate.app_id))).scalars().all())
    total=0
    for app_id in app_ids:
        days,_=await retention(db,app_id)
        cutoff=datetime.now(UTC).replace(tzinfo=None)-timedelta(days=days)
        for _ in range(10):
            removed=await delete_log_batch(db,app_id,cutoff)
            total+=removed
            if removed<1000: break
    return total

async def reset_billing_cycles_and_check_expirations(db: AsyncSession) -> Dict[str, int]:
    """
    1. Resets monthly verification counts when an estate enters a new 30-day billing cycle.
    2. Transitions subscriptions past their expiry date from 'active' to 'expired'.
    """
    today = date.today()
    cycles_reset = 0
    subscriptions_expired = 0

    result = await db.execute(
        select(Subscription).options(selectinload(Subscription.estate))
    )
    subscriptions = result.scalars().all()

    for sub in subscriptions:
        app_id = sub.estate.app_id if sub.estate else None

        # 1. Check if 30 days have elapsed since billing_cycle_start
        if sub.billing_cycle_start and (today - sub.billing_cycle_start).days >= 30:
            sub.current_month_verifications = 0
            sub.billing_cycle_start = today
            cycles_reset += 1
            if app_id:
                await invalidate_subscription_cache(app_id)
                await reset_monthly_quota_redis(app_id)

        # 2. Check if subscription has expired
        if sub.status == "active" and sub.expiry_date and sub.expiry_date < today:
            sub.status = "expired"
            subscriptions_expired += 1
            if app_id:
                await invalidate_subscription_cache(app_id)

    await db.commit()
    logger.info(
        f"[MAINTENANCE] Billing maintenance: {cycles_reset} quota cycles reset, {subscriptions_expired} subscriptions marked expired."
    )
    return {
        "cycles_reset": cycles_reset,
        "subscriptions_expired": subscriptions_expired
    }

async def cleanup_expired_tokens(db: AsyncSession) -> Dict[str,int]:
    """Bound updates and remove only terminal tokens outside estate retention."""
    from app.services.history_policy import retention,BATCH_SIZE
    now=datetime.now(UTC).replace(tzinfo=None)
    expired_ids=select(VisitorToken.id).where(VisitorToken.status=='pending',VisitorToken.expires_at<now).order_by(VisitorToken.expires_at,VisitorToken.id).limit(BATCH_SIZE).with_for_update(skip_locked=True)
    expired=(await db.execute(update(VisitorToken).where(VisitorToken.id.in_(expired_ids)).values(status='expired').execution_options(synchronize_session=False))).rowcount
    await db.commit()
    exit_ids=select(VisitorToken.id).where(VisitorToken.bookout_code.is_not(None),VisitorToken.bookout_expires_at<now).order_by(VisitorToken.bookout_expires_at,VisitorToken.id).limit(BATCH_SIZE).with_for_update(skip_locked=True)
    cleared=(await db.execute(update(VisitorToken).where(VisitorToken.id.in_(exit_ids)).values(bookout_code=None,bookout_expires_at=None).execution_options(synchronize_session=False))).rowcount
    await db.commit()
    app_ids=list((await db.execute(select(Estate.app_id))).scalars().all())
    purged=0
    for app_id in app_ids:
        days,_=await retention(db,app_id)
        cutoff=now-timedelta(days=days)
        terminal=select(VisitorToken.id).where(VisitorToken.app_id==app_id,
            VisitorToken.status.in_(['expired','checked_out']),VisitorToken.expires_at<cutoff,
            ((VisitorToken.checked_out_at.is_(None)) | (VisitorToken.checked_out_at<cutoff)))
        terminal=terminal.order_by(VisitorToken.expires_at,VisitorToken.id).limit(BATCH_SIZE).with_for_update(skip_locked=True)
        purged+=(await db.execute(delete(VisitorToken).where(VisitorToken.id.in_(terminal)).execution_options(synchronize_session=False))).rowcount
        await db.commit()
    return {'pending_tokens_expired':expired,'bookout_codes_cleared':cleared,'terminal_tokens_purged':purged}

async def sync_redis_quotas_to_db(db: AsyncSession) -> int:
    """
    Flush Redis monthly verification counters into Postgres so billing dashboards
    stay accurate without contending on the hot path.
    """
    synced = 0
    app_ids = await list_active_quota_app_ids()
    for app_id in app_ids:
        count = await get_monthly_quota_redis(app_id)
        if count is None:
            continue
        result = await db.execute(
            select(Subscription).join(Estate, Estate.id == Subscription.estate_id).where(Estate.app_id == app_id)
        )
        sub = result.scalar_one_or_none()
        if not sub:
            continue
        sub.current_month_verifications = count
        synced += 1

    if synced:
        await db.commit()
    logger.info(f"[MAINTENANCE] Synced {synced} Redis quota counters into Postgres.")
    return synced


async def run_all_maintenance_jobs(db: AsyncSession) -> Dict[str, Any]:
    """Executes all scheduled background maintenance jobs in sequence."""
    logger.info("[MAINTENANCE] Starting automated platform maintenance run...")
    start_time = datetime.now(UTC).replace(tzinfo=None)

    drained_logs = await drain_access_log_queue(db)
    drained_token_states = await drain_token_state_queue(db)
    drained_chat_messages = await drain_chat_message_queue(db)
    purged_logs = await purge_expired_access_logs(db)
    billing_stats = await reset_billing_cycles_and_check_expirations(db)
    quotas_synced = await sync_redis_quotas_to_db(db)
    token_stats = await cleanup_expired_tokens(db)

    duration = (datetime.now(UTC).replace(tzinfo=None) - start_time).total_seconds()
    logger.info(f"[MAINTENANCE] Platform maintenance run completed in {duration:.2f}s.")

    return {
        "timestamp": start_time.isoformat(),
        "duration_seconds": round(duration, 2),
        "access_logs_drained": drained_logs,
        "token_states_drained": drained_token_states,
        "chat_messages_drained": drained_chat_messages,
        "access_logs_purged": purged_logs,
        "quotas_synced": quotas_synced,
        "billing_cycles_reset": billing_stats["cycles_reset"],
        "subscriptions_expired": billing_stats["subscriptions_expired"],
        "pending_tokens_expired": token_stats["pending_tokens_expired"],
        "bookout_codes_cleared": token_stats["bookout_codes_cleared"],
        "status": "success"
    }
