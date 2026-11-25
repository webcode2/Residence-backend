"""Outbound notifications (FCM pushes and alert emails) go through the worker.

Requests call `dispatch(kind, **args)` and return immediately. With the Cloud Run worker
configured (WORKER_WAKE_URL), the job is pushed to a Redis list and the worker is woken
to send it; failed sends are retried up to MAX_ATTEMPTS. Without a worker configured
(local / docker compose) the notification is sent before the request returns.

Kinds:
  push          one FCM message       {token, title, body, data, high_priority}
  push_many     FCM to several tokens {tokens, title, body, data, high_priority}
  email_alert   real-time alert email {email, title, message}
  chat_push     chat FCM fallback     {dispatch: ChatDispatch as dict}
  settings_sync silent estate-settings invalidation push {tokens, app_id}
  staff_welcome security staff credentials email {email, password_ciphertext, full_name,
                app_id, idempotency_key}; the password is Fernet-encrypted while queued
"""
import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Dict

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

NOTIFY_QUEUE_KEY = "residence:notify:queue"
MAX_ATTEMPTS = 3


async def _run(kind: str, args: Dict[str, Any]) -> bool:
    """Send one notification. Returns True when the provider accepted it."""
    from app.services.fcm import fcm_service
    from app.services.notifications import notification_service
    if kind == "push":
        from app.services.fcm import PushStatus
        result = await fcm_service.send_to_token(args.get("token"), args["title"], args["body"],
                                                 data=args.get("data"), high_priority=args.get("high_priority", True))
        # Only transient failures are worth retrying; a stale or missing token never recovers.
        return getattr(result, "status", None) != PushStatus.FAILED
    if kind == "push_many":
        await fcm_service.send_to_tokens(args.get("tokens") or [], args["title"], args["body"],
                                         args.get("data"), high_priority=args.get("high_priority", True))
        return True
    if kind == "email_alert":
        return bool(await notification_service.send_realtime_access_alert(args["email"], args["title"], args["message"]))
    if kind == "staff_welcome":
        from app.services.landlords import cipher
        password = cipher().decrypt(args["password_ciphertext"].encode()).decode()
        return bool(await notification_service.send_staff_welcome_email(
            args["email"], password, args.get("full_name", ""), args.get("app_id", ""),
            idempotency_key=args.get("idempotency_key")))
    if kind == "settings_sync":
        from app.services import estate_settings_sync
        await estate_settings_sync.notify(args.get("tokens") or [], args["app_id"])
        return True
    if kind == "chat_push":
        from app.services.chat import ChatDispatch, PushTarget, send_push_notifications
        plan = dict(args["dispatch"])
        plan["push_targets"] = [PushTarget(**t) for t in plan.get("push_targets", [])]
        await send_push_notifications(ChatDispatch(**plan))
        return True
    logger.error("unknown notification kind %r dropped", kind)
    return True


def _encode_args(kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if kind == "chat_push" and not isinstance(args.get("dispatch"), dict):
        return {"dispatch": asdict(args["dispatch"])}
    return args


async def dispatch(kind: str, **args: Any) -> None:
    """Queue a notification for the worker (or send it in-process without one). Never raises."""
    from app.services.worker_wake import wake_enabled, wake_worker
    payload = _encode_args(kind, args)
    if wake_enabled():
        client = get_redis_client()
        if client is not None:
            try:
                await client.rpush(NOTIFY_QUEUE_KEY, json.dumps({"kind": kind, "args": payload, "attempts": 0}, default=str))
                await wake_worker("notification")
                return
            except Exception:
                logger.warning("notification queue unavailable; sending %s in-request", kind, exc_info=True)
        try:
            await _run(kind, payload)  # last resort: never drop a notification
        except Exception:
            logger.exception("in-request %s notification failed", kind)
        return
    await _safe_run(kind, payload)


async def _safe_run(kind: str, args: Dict[str, Any]) -> bool:
    try:
        return await _run(kind, args)
    except Exception:
        logger.exception("%s notification failed", kind)
        return False


async def drain_notification_queue(batch_size: int = 100) -> int:
    """Worker: send queued notifications concurrently; re-queue failures up to MAX_ATTEMPTS."""
    client = get_redis_client()
    if client is None:
        return 0
    raw_items = await client.lpop(NOTIFY_QUEUE_KEY, batch_size)
    if not raw_items:
        return 0
    jobs = []
    for raw in raw_items:
        try:
            jobs.append(json.loads(raw))
        except ValueError:
            logger.error("unreadable notification job dropped")
    semaphore = asyncio.Semaphore(10)

    async def send(job):
        async with semaphore:
            return await _safe_run(job.get("kind", ""), job.get("args") or {})

    results = await asyncio.gather(*(send(job) for job in jobs))
    retry = []
    for job, ok in zip(jobs, results):
        if not ok and job.get("attempts", 0) + 1 < MAX_ATTEMPTS:
            retry.append(json.dumps({**job, "attempts": job.get("attempts", 0) + 1}, default=str))
        elif not ok:
            logger.error("notification %s gave up after %s attempts", job.get("kind"), MAX_ATTEMPTS)
    if retry:
        await client.rpush(NOTIFY_QUEUE_KEY, *retry)
    return len(jobs)
