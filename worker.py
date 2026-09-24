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

async def run_daemon():
    """Runs continuous maintenance on interval."""
    setup_logging()
    interval = settings.MAINTENANCE_INTERVAL_SECONDS
    logger.info(f"Starting standalone maintenance worker daemon (Interval: {interval}s)...")
    try:
        while True:
            try:
                async with SessionLocal() as db:
                    await run_all_maintenance_jobs(db)
            except Exception as e:
                logger.error(f"Error during daemon maintenance cycle: {e}", exc_info=True)

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Maintenance worker daemon shutting down...")
    finally:
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
