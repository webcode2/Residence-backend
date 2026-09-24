"""
Standalone CLI maintenance worker and cron runner for Residence SaaS.
Usage:
    python worker.py          # Runs maintenance jobs once and exits (Ideal for CronJobs)
    python worker.py --daemon # Runs continuously as an independent background worker
"""
import sys
import asyncio
import logging
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.redis import close_redis
from app.core.logging import setup_logging
from app.services.maintenance import run_all_maintenance_jobs
from app.services.access_logs import drain_access_log_queue
from app.services.token_state import drain_token_state_queue
# Register all ORM models so FK resolution works during batch inserts
from app.models import estate, user, token, access_log  # noqa: F401

logger = logging.getLogger("worker")


async def run_once() -> int:
    """Executes a single maintenance run across all tenants."""
    setup_logging()
    logger.info("Executing scheduled maintenance pass...")
    try:
        async with SessionLocal() as db:
            result = await run_all_maintenance_jobs(db)
            logger.info(f"Maintenance pass succeeded: {result}")
            return 0
    except Exception as e:
        logger.error(f"Maintenance pass failed: {e}", exc_info=True)
        return 1
    finally:
        await close_redis()
        await engine.dispose()


async def _queue_drain_loop(stop_event: asyncio.Event):
    """
    Buffer Redis queues for ~2s, then flush to Postgres in batches
    (access logs via add_all, token states via coalesced executemany UPDATEs).
    """
    interval = float(
        getattr(settings, "TOKEN_STATE_DRAIN_INTERVAL_SECONDS", None)
        or settings.ACCESS_LOG_DRAIN_INTERVAL_SECONDS
    )
    logger.info(f"Queue drain loop started (batch interval={interval}s)")
    while not stop_event.is_set():
        # Accumulate events for the interval before draining
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop requested
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break

        try:
            async with SessionLocal() as db:
                logs = await drain_access_log_queue(db)
                tokens = await drain_token_state_queue(db)
                if logs or tokens:
                    logger.info(f"Batch drain complete: access_logs={logs} token_states={tokens}")
        except Exception as e:
            logger.error(f"Queue drain error: {e}", exc_info=True)


async def _maintenance_loop(stop_event: asyncio.Event):
    interval = settings.MAINTENANCE_INTERVAL_SECONDS
    logger.info(f"Maintenance loop started (interval={interval}s)")
    while not stop_event.is_set():
        try:
            async with SessionLocal() as db:
                await run_all_maintenance_jobs(db)
        except Exception as e:
            logger.error(f"Error during daemon maintenance cycle: {e}", exc_info=True)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break


async def run_daemon():
    """Runs continuous queue draining + periodic maintenance."""
    setup_logging()
    stop_event = asyncio.Event()
    logger.info("Starting standalone worker daemon (queue drain + maintenance)...")
    try:
        await asyncio.gather(
            _queue_drain_loop(stop_event),
            _maintenance_loop(stop_event),
        )
    except asyncio.CancelledError:
        logger.info("Worker daemon shutting down...")
        stop_event.set()
    finally:
        stop_event.set()
        await close_redis()
        await engine.dispose()


def main():
    if "--daemon" in sys.argv:
        try:
            asyncio.run(run_daemon())
        except KeyboardInterrupt:
            print("Daemon stopped by user.")
    else:
        exit_code = asyncio.run(run_once())
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
