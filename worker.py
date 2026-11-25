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
from app.services.chat import drain_chat_message_queue
from app.services.landlords import _run as landlord_onboarding_loop
from app.services.sos import _loop as sos_outbox_loop
from app.services.history_jobs import _run as history_jobs_loop
# Register all ORM models so FK resolution works during batch inserts
from app.models import estate, user, token, access_log, chat, onboarding, history, sos  # noqa: F401

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
    (access logs via add_all, token states via coalesced executemany UPDATEs, chat messages via batch insert).
    """
    interval = float(
        getattr(settings, "CHAT_MESSAGE_DRAIN_INTERVAL_SECONDS", None)
        or getattr(settings, "TOKEN_STATE_DRAIN_INTERVAL_SECONDS", None)
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
                chat_msgs = await drain_chat_message_queue(db)
                if logs or tokens or chat_msgs:
                    logger.info(f"Batch drain complete: access_logs={logs} token_states={tokens} chat_messages={chat_msgs}")
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
    """Runs continuous queue draining, maintenance, and durable job workers."""
    setup_logging()
    stop_event = asyncio.Event()
    logger.info(
        "Starting standalone worker daemon (queue drain + maintenance + landlord onboarding + sos outbox + history jobs)..."
    )
    tasks = [
        asyncio.create_task(_queue_drain_loop(stop_event), name="worker.queue_drain"),
        asyncio.create_task(_maintenance_loop(stop_event), name="worker.maintenance"),
        asyncio.create_task(landlord_onboarding_loop(), name="worker.landlord_onboarding"),
        asyncio.create_task(sos_outbox_loop(), name="worker.sos_outbox"),
        asyncio.create_task(history_jobs_loop(), name="worker.history_jobs"),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Worker daemon shutting down...")
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
