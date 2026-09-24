import asyncio
import logging
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis import get_redis_client
from app.services.maintenance import run_all_maintenance_jobs

logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None
_stop_event = asyncio.Event()

async def _maintenance_worker_loop():
    """Continuous background loop executing automated maintenance tasks on interval."""
    logger.info(
        f"[SCHEDULER] Maintenance background worker started. Interval: {settings.MAINTENANCE_INTERVAL_SECONDS}s"
    )
    
    # Brief initial pause on application boot so DB/Redis are fully ready
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        return

    while not _stop_event.is_set():
        try:
            redis = await get_redis_client()
            lock_key = "residence:maintenance:lock"
            lock_ttl = min(int(settings.MAINTENANCE_INTERVAL_SECONDS), 300)
            # Acquire distributed lock so only one worker process runs maintenance across multiple instances
            acquired = await redis.set(lock_key, "locked", nx=True, ex=lock_ttl)
            if acquired:
                try:
                    async with SessionLocal() as db:
                        await run_all_maintenance_jobs(db)
                finally:
                    try:
                        await redis.delete(lock_key)
                    except Exception:
                        pass
            else:
                logger.debug("[SCHEDULER] Maintenance lock currently held by another worker. Skipping.")
        except Exception as e:
            logger.error(f"[SCHEDULER] Unhandled exception in maintenance background worker: {e}", exc_info=True)

        try:
            # Sleep until next scheduled interval or until stop event is fired
            await asyncio.wait_for(_stop_event.wait(), timeout=float(settings.MAINTENANCE_INTERVAL_SECONDS))
        except asyncio.TimeoutError:
            # Expected: timeout indicates interval elapsed, proceed to next run
            continue
        except asyncio.CancelledError:
            break

    logger.info("[SCHEDULER] Maintenance background worker loop cleanly stopped.")

def start_maintenance_scheduler():
    """Spawns the background maintenance worker task if enabled."""
    global _scheduler_task, _stop_event
    if not settings.ENABLE_MAINTENANCE_SCHEDULER:
        logger.info("[SCHEDULER] Automated maintenance scheduler is disabled in configuration.")
        return

    if _scheduler_task is None or _scheduler_task.done():
        _stop_event.clear()
        _scheduler_task = asyncio.create_task(_maintenance_worker_loop(), name="residence_maintenance_worker")

async def stop_maintenance_scheduler():
    """Cancels and awaits the background maintenance worker task on application shutdown."""
    global _scheduler_task, _stop_event
    _stop_event.set()
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        _scheduler_task = None
