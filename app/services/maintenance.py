import logging
from datetime import datetime, timedelta, date, UTC
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from sqlalchemy.orm import selectinload

from app.models.estate import Estate, Subscription
from app.models.access_log import AccessLog
from app.models.token import VisitorToken
from app.core.redis import invalidate_subscription_cache

logger = logging.getLogger(__name__)

async def purge_expired_access_logs(db: AsyncSession) -> int:
    """
    Purges access logs that exceed each estate's subscription tier retention window
    (30 days for Starter, 90 days for Standard, 180 days for Premium, 365 days for Enterprise).
    """
    total_purged = 0
    now = datetime.now(UTC).replace(tzinfo=None)

    # 1. Fetch all estates with their active subscription details
    result = await db.execute(
        select(Estate).options(selectinload(Estate.subscription))
    )
    estates = result.scalars().all()

    for estate in estates:
        if not estate.app_id:
            continue

        retention_days = 90 # fallback
        if estate.subscription and estate.subscription.log_retention_days:
            retention_days = estate.subscription.log_retention_days

        cutoff = now - timedelta(days=retention_days)

        # 2. Delete logs older than retention window for this specific tenant
        del_stmt = (
            delete(AccessLog)
            .where(
                AccessLog.app_id == estate.app_id,
                AccessLog.created_at < cutoff
            )
        )
        del_res = await db.execute(del_stmt)
        purged_for_estate = del_res.rowcount
        total_purged += purged_for_estate

    await db.commit()
    logger.info(f"[MAINTENANCE] Access log retention purge completed. Purged {total_purged} expired log entries.")
    return total_purged

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

async def cleanup_expired_tokens(db: AsyncSession) -> Dict[str, int]:
    """
    1. Transitions unverified visitor entry tokens past their 24h validity window to 'expired'.
    2. Clears expired book-out exit passcodes.
    """
    now = datetime.now(UTC).replace(tzinfo=None)

    # 1. Expire stale pending entry tokens
    entry_stmt = (
        update(VisitorToken)
        .where(
            VisitorToken.status == "pending",
            VisitorToken.expires_at < now
        )
        .values(status="expired")
    )
    entry_res = await db.execute(entry_stmt)
    pending_tokens_expired = entry_res.rowcount

    # 2. Clear stale book-out exit codes
    bookout_stmt = (
        update(VisitorToken)
        .where(
            VisitorToken.bookout_code.isnot(None),
            VisitorToken.bookout_expires_at < now
        )
        .values(bookout_code=None, bookout_expires_at=None)
    )
    bookout_res = await db.execute(bookout_stmt)
    bookout_codes_cleared = bookout_res.rowcount

    await db.commit()
    logger.info(
        f"[MAINTENANCE] Token cleanup: {pending_tokens_expired} pending tokens marked expired, {bookout_codes_cleared} stale exit codes cleared."
    )
    return {
        "pending_tokens_expired": pending_tokens_expired,
        "bookout_codes_cleared": bookout_codes_cleared
    }

async def run_all_maintenance_jobs(db: AsyncSession) -> Dict[str, Any]:
    """Executes all scheduled background maintenance jobs in sequence."""
    logger.info("[MAINTENANCE] Starting automated platform maintenance run...")
    start_time = datetime.now(UTC).replace(tzinfo=None)

    purged_logs = await purge_expired_access_logs(db)
    billing_stats = await reset_billing_cycles_and_check_expirations(db)
    token_stats = await cleanup_expired_tokens(db)

    duration = (datetime.now(UTC).replace(tzinfo=None) - start_time).total_seconds()
    logger.info(f"[MAINTENANCE] Platform maintenance run completed in {duration:.2f}s.")

    return {
        "timestamp": start_time.isoformat(),
        "duration_seconds": round(duration, 2),
        "access_logs_purged": purged_logs,
        "billing_cycles_reset": billing_stats["cycles_reset"],
        "subscriptions_expired": billing_stats["subscriptions_expired"],
        "pending_tokens_expired": token_stats["pending_tokens_expired"],
        "bookout_codes_cleared": token_stats["bookout_codes_cleared"],
        "status": "success"
    }
