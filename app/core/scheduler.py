import asyncio
import logging
import uuid
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis import get_redis_client
from app.services.maintenance import run_all_maintenance_jobs

logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None
_stop_event = asyncio.Event()

async def _run_with_lease(redis,lock_key,lock_token,lock_ttl,operation):
    """Keep ownership for long sweeps; never delete a successor's lock."""
    async def renew():
        while True:
            await asyncio.sleep(lock_ttl/3)
            renewed=await redis.eval("if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end",1,lock_key,lock_token,lock_ttl)
            if not renewed:
                raise RuntimeError('Maintenance lease lost; stopping this worker')
    work=asyncio.create_task(operation())
    renewal=asyncio.create_task(renew())
    try:
        done,_=await asyncio.wait({work,renewal},return_when=asyncio.FIRST_COMPLETED)
        for task in done: await task
    finally:
        work.cancel();renewal.cancel()
        await asyncio.gather(work,renewal,return_exceptions=True)
        try:
            await redis.eval("if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",1,lock_key,lock_token)
        except Exception:
            pass


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
            lock_ttl = max(30,min(int(settings.MAINTENANCE_INTERVAL_SECONDS),300))
            lock_token = uuid.uuid4().hex
            # Acquire distributed lock so only one worker process runs maintenance across multiple instances
            acquired = await redis.set(lock_key, lock_token, nx=True, ex=lock_ttl)
            if acquired:
                async def perform():
                    async with SessionLocal() as db:
                        await run_all_maintenance_jobs(db)
                await _run_with_lease(redis,lock_key,lock_token,lock_ttl,perform)
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
