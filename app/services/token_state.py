"""
Async persistence for visitor token check-in / check-out state.

Gate verify claims the code in Redis (one-time), sets live status immediately,
and enqueues the Postgres UPDATE for the worker — keeping the hot path off DB writes.

The worker buffers ~2s then flushes coalesced batches with executemany UPDATEs.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any, Optional, Dict, List, Tuple
import uuid

from sqlalchemy import update, bindparam
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.token import VisitorToken
from app.core.config import settings
from app.core.redis import (
    enqueue_token_state_update,
    dequeue_token_state_updates,
    requeue_token_state_updates,
    set_token_live_status,
    claim_token_code,
)

logger = logging.getLogger(__name__)


def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            pass
    return datetime.now(UTC).replace(tzinfo=None)


async def persist_token_check_in(
    db: AsyncSession,
    *,
    token_id: Any,
    app_id: str,
    code: str,
    at: Optional[datetime] = None,
    already_claimed: bool = False,
) -> bool:
    """
    Mark visitor token checked-in.
    Returns False if the code was already claimed (duplicate verify).
    """
    now = at or datetime.now(UTC).replace(tzinfo=None)
    tid = str(token_id)

    if not already_claimed:
        claimed = await claim_token_code(app_id, code)
        if not claimed:
            return False

    await set_token_live_status(
        tid,
        "checked_in",
        extra={"checked_in_at": now.isoformat(), "app_id": app_id, "code": code},
    )

    payload = {
        "action": "check_in",
        "token_id": tid,
        "app_id": app_id,
        "code": code,
        "at": now.isoformat(),
    }

    if await enqueue_token_state_update(payload):
        return True

    # Redis unavailable: synchronous Postgres fallback
    await db.execute(
        update(VisitorToken)
        .where(VisitorToken.id == _parse_uuid(tid))
        .values(is_used=True, status="checked_in", checked_in_at=now)
    )
    await db.commit()
    return True


async def persist_token_check_out(
    db: AsyncSession,
    *,
    token_id: Any,
    app_id: str,
    code: str,
    at: Optional[datetime] = None,
    already_claimed: bool = False,
) -> bool:
    """Mark visitor token checked-out. Returns False on duplicate claim."""
    now = at or datetime.now(UTC).replace(tzinfo=None)
    tid = str(token_id)

    if not already_claimed:
        claimed = await claim_token_code(app_id, code)
        if not claimed:
            return False

    await set_token_live_status(
        tid,
        "checked_out",
        extra={"checked_out_at": now.isoformat(), "app_id": app_id, "code": code},
    )

    payload = {
        "action": "check_out",
        "token_id": tid,
        "app_id": app_id,
        "code": code,
        "at": now.isoformat(),
    }

    if await enqueue_token_state_update(payload):
        return True

    await db.execute(
        update(VisitorToken)
        .where(VisitorToken.id == _parse_uuid(tid))
        .values(is_used=True, status="checked_out", checked_out_at=now)
    )
    await db.commit()
    return True


def _coalesce_token_updates(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Collapse queued events per token_id (last event wins).
    check_out also forces is_used=True so a skipped check_in is still safe.
    Returns (check_in_rows, check_out_rows) for executemany.
    """
    latest: Dict[uuid.UUID, Dict[str, Any]] = {}
    for item in items:
        token_id = _parse_uuid(item.get("token_id"))
        if not token_id:
            continue
        action = item.get("action")
        if action not in ("check_in", "check_out"):
            continue
        latest[token_id] = {
            "b_id": token_id,
            "b_at": _parse_at(item.get("at")),
            "action": action,
        }

    check_ins: List[Dict[str, Any]] = []
    check_outs: List[Dict[str, Any]] = []
    for row in latest.values():
        if row["action"] == "check_in":
            check_ins.append({"b_id": row["b_id"], "b_at": row["b_at"]})
        else:
            check_outs.append({"b_id": row["b_id"], "b_at": row["b_at"]})
    return check_ins, check_outs


async def drain_token_state_queue(
    db: AsyncSession,
    batch_size: Optional[int] = None,
) -> int:
    """
    Pop a Redis batch (accumulated over the drain interval), coalesce per token,
    and apply two executemany UPDATEs (check-ins, then check-outs).
    """
    size = batch_size or settings.TOKEN_STATE_DRAIN_BATCH_SIZE
    dequeued = await dequeue_token_state_updates(size)
    if not dequeued:
        return 0

    raw_items = [raw for raw, _ in dequeued]
    parsed = [item for _raw, item in dequeued]

    try:
        check_ins, check_outs = _coalesce_token_updates(parsed)
        applied = 0

        if check_ins:
            table = VisitorToken.__table__
            stmt = (
                update(table)
                .where(table.c.id == bindparam("b_id"))
                .values(
                    is_used=True,
                    status="checked_in",
                    checked_in_at=bindparam("b_at"),
                )
            )
            await db.execute(stmt, check_ins)
            applied += len(check_ins)

        if check_outs:
            table = VisitorToken.__table__
            stmt = (
                update(table)
                .where(table.c.id == bindparam("b_id"))
                .values(
                    is_used=True,
                    status="checked_out",
                    checked_out_at=bindparam("b_at"),
                )
            )
            await db.execute(stmt, check_outs)
            applied += len(check_outs)

        await db.commit()
        logger.info(
            f"Batch-flushed token state: queued={len(parsed)} "
            f"coalesced_in={len(check_ins)} coalesced_out={len(check_outs)}"
        )
        return applied
    except Exception:
        await db.rollback()
        await requeue_token_state_updates(raw_items)
        raise
