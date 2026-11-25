"""Wake the scale-to-zero worker service after work has been queued.

The API never does background work itself: it puts the work on a queue (Redis lists
for access logs / visitor-token state / chat, durable Postgres job tables for SOS
pushes, welcome emails and history exports) and calls `wake_worker()`. That creates a
Google Cloud Tasks task which POSTs to the worker's /internal/drain endpoint with an
OIDC token, starting the worker if it is asleep. The worker drains everything due,
schedules a wake for the next retry time, and scales back to zero.

A short-lived Redis flag collapses bursts (many gate scans in a second) into a single
wake. Ordering keeps this race-free: producers enqueue *before* setting the flag, and
the worker clears the flag *before* its final empty pass.

When WORKER_WAKE_URL is not configured (local docker compose), this is a no-op and the
embedded or dedicated worker processes poll as before.
"""
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

WAKE_FLAG_KEY = "residence:worker:wake_pending"
WAKE_FLAG_TTL_SECONDS = 5
_METADATA_TOKEN_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                       "service-accounts/default/token")
_token: Optional[str] = None
_token_expires_at = 0.0


def wake_enabled() -> bool:
    return bool(settings.WORKER_WAKE_URL and settings.WORKER_TASKS_QUEUE and settings.WORKER_TASKS_INVOKER_SA)


async def _access_token(client: httpx.AsyncClient) -> str:
    """Service-account token from the Cloud Run metadata server, cached until near expiry."""
    global _token, _token_expires_at
    if _token and time.monotonic() < _token_expires_at:
        return _token
    response = await client.get(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=3.0)
    response.raise_for_status()
    data = response.json()
    _token = data["access_token"]
    _token_expires_at = time.monotonic() + max(60, int(data.get("expires_in", 300)) - 60)
    return _token


async def _create_task(at: Optional[datetime], reason: str) -> None:
    worker_url = settings.WORKER_WAKE_URL.rstrip("/")
    audience = worker_url.split("/internal/")[0]
    body = json.dumps({"reason": reason}).encode()
    task: dict = {
        "httpRequest": {
            "httpMethod": "POST",
            "url": worker_url,
            "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(body).decode(),
            "oidcToken": {"serviceAccountEmail": settings.WORKER_TASKS_INVOKER_SA, "audience": audience},
        },
        "dispatchDeadline": f"{settings.WORKER_DRAIN_BUDGET_SECONDS + 120}s",
    }
    if at is not None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        task["scheduleTime"] = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    async with httpx.AsyncClient() as client:
        token = await _access_token(client)
        response = await client.post(
            f"https://cloudtasks.googleapis.com/v2/{settings.WORKER_TASKS_QUEUE}/tasks",
            json={"task": task}, headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        response.raise_for_status()


async def wake_worker(reason: str, at: Optional[datetime] = None) -> bool:
    """Ask the worker to drain queued work now (or at `at` for retries). Never raises.

    Call this *after* the work has been committed / pushed to its queue.
    Returns True when a wake task was created.
    """
    if not wake_enabled():
        return False
    flag_set = False
    client = get_redis_client() if at is None else None
    if client is not None:
        try:
            flag_set = bool(await client.set(WAKE_FLAG_KEY, reason, nx=True, ex=WAKE_FLAG_TTL_SECONDS))
            if not flag_set:
                return False  # a wake is already pending; the worker will see this work
        except Exception:
            logger.debug("wake flag unavailable; creating a wake task anyway", exc_info=True)
    try:
        await _create_task(at, reason)
        return True
    except Exception:
        logger.exception("worker wake failed (reason=%s); work stays queued for the next wake", reason)
        if flag_set:
            try:
                await client.delete(WAKE_FLAG_KEY)
            except Exception:
                pass
        return False


async def clear_wake_flag() -> None:
    """Worker: allow new wakes. Must run before the worker's final check for work."""
    client = get_redis_client()
    if client is None:
        return
    try:
        await client.delete(WAKE_FLAG_KEY)
    except Exception:
        logger.debug("could not clear wake flag", exc_info=True)
