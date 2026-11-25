"""Residence worker service: drains queued background work, then sleeps.

Deployed as a separate, private Cloud Run service (scale to zero, one instance).
Google Cloud Tasks wakes it by POSTing /internal/drain (see app/services/worker_wake.py);
Cloud Run IAM only lets the tasks service account invoke it.

Each drain request processes, until nothing is due:
  * Redis queues: notifications (FCM pushes, alert emails), gate access logs,
    visitor-token state, chat messages
  * durable Postgres job tables: SOS push deliveries, host / property owner welcome
    emails, history exports / purges
then schedules a wake for the earliest future retry, so retries happen without anything
running in between.

    gunicorn app.worker_app:app -w 1 -k uvicorn.workers.UvicornWorker --timeout 0
"""
import logging
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Optional

from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.logging import setup_logging
from app.core.redis import close_redis
from app.models import access_log, chat, estate, history, onboarding, sos as sos_models, token, user  # noqa: F401
from app.models.base import utc_now

logger = logging.getLogger("worker")

MAX_JOBS_PER_PASS = 25


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    logger.info("Residence worker ready (drain budget %ss)", settings.WORKER_DRAIN_BUDGET_SECONDS)
    yield
    await close_redis()
    await engine.dispose()


app = FastAPI(title="Residence worker", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def _repeat(step, limit: int) -> int:
    done = 0
    while done < limit:
        async with SessionLocal() as db:
            if not await step(db):
                break
        done += 1
    return done


async def _one_pass(totals: Counter) -> int:
    """Process everything currently due once. Returns how many items were handled."""
    from app.services import history_jobs, landlords, sos
    from app.services.access_logs import drain_access_log_queue
    from app.services.chat import drain_chat_message_queue
    from app.services.notification_queue import drain_notification_queue
    from app.services.token_state import drain_token_state_queue

    handled = 0

    async def run(name, coro_factory):
        nonlocal handled
        try:
            n = int(await coro_factory() or 0)
        except Exception:
            logger.exception("worker %s failed; items stay queued for retry", name)
            return
        totals[name] += n
        handled += n

    async def with_session(fn):
        async with SessionLocal() as db:
            return await fn(db)

    # SOS first: it is the time-critical one.
    await run("sos_pushes", lambda: with_session(sos.drain))
    await run("notifications", drain_notification_queue)
    await run("access_logs", lambda: with_session(drain_access_log_queue))
    await run("token_states", lambda: with_session(drain_token_state_queue))
    await run("chat_messages", lambda: with_session(drain_chat_message_queue))
    await run("welcome_emails", lambda: _repeat(landlords.process_one, MAX_JOBS_PER_PASS))
    await run("history_jobs", lambda: _repeat(history_jobs.process_one, MAX_JOBS_PER_PASS))
    return handled


async def _next_due() -> Optional[object]:
    """Earliest future retry across the durable job tables (within 24 hours)."""
    from app.models.history import HistoryJob
    from app.models.onboarding import LandlordOnboardingJob as Job
    from app.models.sos import SOSDelivery
    now, horizon = utc_now(), utc_now() + timedelta(hours=24)
    async with SessionLocal() as db:
        candidates = [
            await db.scalar(select(func.min(SOSDelivery.next_attempt_at)).where(
                SOSDelivery.status.in_(["pending", "failed", "awaiting_receipt", "report_pending"]),
                SOSDelivery.attempts < 6, SOSDelivery.next_attempt_at > now)),
            await db.scalar(select(func.min(Job.next_attempt_at)).where(
                Job.status.in_(["queued", "retrying"]), Job.next_attempt_at > now)),
            await db.scalar(select(func.min(Job.lease_until)).where(
                Job.status == "processing", Job.lease_until > now)),
            await db.scalar(select(func.min(HistoryJob.next_attempt_at)).where(
                HistoryJob.status.in_(["queued", "processing", "previewing"]), HistoryJob.next_attempt_at > now)),
        ]
    due = [c for c in candidates if c is not None and c <= horizon]
    return min(due) if due else None


async def drain_all(budget_seconds: float) -> dict:
    from app.services.worker_wake import clear_wake_flag, wake_worker
    deadline = time.monotonic() + budget_seconds
    totals: Counter = Counter()
    passes = 0
    out_of_time = False
    while True:
        if time.monotonic() >= deadline:
            out_of_time = True
            break
        passes += 1
        if await _one_pass(totals):
            continue
        # Nothing left: re-open wakes, then look once more so work queued while the
        # flag was set is never stranded.
        await clear_wake_flag()
        if not await _one_pass(totals):
            break
    if out_of_time:
        await wake_worker("continue")  # more work than one request's budget
        next_wake = "now"
    else:
        due = await _next_due()
        next_wake = due.isoformat() + "Z" if due else None
        if due is not None:
            await wake_worker("retry", at=due + timedelta(seconds=1))
    result = {"passes": passes, "handled": dict(totals), "next_wake": next_wake}
    logger.info("worker drain finished: %s", result)
    return result


@app.post("/internal/drain")
async def drain():
    return await drain_all(settings.WORKER_DRAIN_BUDGET_SECONDS)


@app.get("/healthz")
async def healthz():
    return {"status": "healthy", "app": "residence-worker"}
