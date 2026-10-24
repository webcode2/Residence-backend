"""
Gatehouse <-> Resident chat business logic.

Rules enforced here (Gatehouse chat spec §1):
  * Only residents (incl. landlords/caretakers living in the estate) can open threads;
    any caller holding UserRole.SECURITY gets 403.
  * Residents address a gate site, never an individual guard. Omitting the gate routes
    to the estate's primary (oldest active) gate.
  * Resident messages fan out to on-duty guards posted at that gate, or to every on-duty
    guard in the estate when nobody is posted there. With nobody on duty at all, every
    security user in the estate is notified so a chat is never silently dropped.
  * Guard replies are stamped with the officer's name, badge and current post so other
    guards see who answered.
  * Push notifications are skipped for recipients currently viewing the thread.
"""
import logging
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException, status
from sqlalchemy import case, inspect as sa_inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.redis import (
    check_rate_limit,
    enqueue_chat_message,
    dequeue_chat_messages,
    requeue_chat_messages,
    chat_message_queue_length,
)
from app.models.base import utc_now
from app.models.chat import ChatMessage, ChatThread, GateSite
from app.models.estate import Estate
from app.models.user import User, UserRole
from app.models.sos import PushDevice
from app.schemas.chat import (
    ChatMessageCreateSchema,
    ChatMessageSchema,
    ChatThreadCreateSchema,
    ChatThreadSchema,
    GateSiteCreateSchema,
    GuardDutyUpdateSchema,
    GuardPresenceSchema,
)
from app.services.chat_websocket import chat_hub, presence_store
from app.services.fcm import PushStatus, fcm_service

logger = logging.getLogger(__name__)

ROLE_RESIDENT = "resident"
ROLE_SECURITY = "security"
ROLE_SYSTEM = "system"

ROUTABLE_DUTY_STATUSES = ("on_duty", "on_break")
SNIPPET_LENGTH = 500

THREAD_CREATE_RATE_LIMIT = (10, 60)
MESSAGE_SEND_RATE_LIMIT = (30, 60)


# --- Helpers ---

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
    return utc_now()


def is_guard(user: User) -> bool:
    return UserRole.SECURITY in (user.roles or [])


def display_name(user: User) -> str:
    return user.full_name or user.email.split("@")[0]


def unit_address(user: User) -> Optional[str]:
    parts = [p for p in (user.house_number, user.street_name) if p]
    return ", ".join(parts) if parts else None


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_LENGTH else text[: SNIPPET_LENGTH - 1] + "…"


def _gate_name(thread: ChatThread) -> Optional[str]:
    if "gate_site" in sa_inspect(thread).unloaded:
        return None
    return thread.gate_site.name if thread.gate_site else None


def thread_to_schema(thread: ChatThread) -> ChatThreadSchema:
    schema = ChatThreadSchema.model_validate(thread)
    schema.gate_site_name = _gate_name(thread)
    return schema


def message_to_schema(message: ChatMessage) -> ChatMessageSchema:
    return ChatMessageSchema.model_validate(message)


async def _enforce_rate_limit(prefix: str, user: User, limit: Tuple[int, int]) -> None:
    allowed, _, retry_after = await check_rate_limit(prefix, str(user.id), max_requests=limit[0], window_seconds=limit[1])
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many chat requests. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


# --- Gate Sites ---

async def _get_estate(db: AsyncSession, app_id: str) -> Estate:
    estate = (await db.execute(select(Estate).where(Estate.app_id == app_id))).scalar_one_or_none()
    if not estate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Estate not found")
    return estate


async def list_gate_sites(db: AsyncSession, app_id: str, include_inactive: bool = False) -> Sequence[GateSite]:
    query = select(GateSite).where(GateSite.app_id == app_id, GateSite.is_deleted.is_(False))
    if not include_inactive:
        query = query.where(GateSite.is_active.is_(True))
    query = query.order_by(GateSite.created_at, GateSite.name)
    return (await db.execute(query)).scalars().all()


async def create_gate_site(db: AsyncSession, app_id: str, data: GateSiteCreateSchema) -> GateSite:
    estate = await _get_estate(db, app_id)
    gate = GateSite(
        app_id=app_id,
        estate_id=estate.id,
        name=data.name.strip(),
        code=data.code.strip() if data.code else None,
        description=data.description,
        is_active=data.is_active,
    )
    db.add(gate)
    await db.commit()
    await db.refresh(gate)
    return gate


async def resolve_gate_site(db: AsyncSession, app_id: str, gate_site_id: Optional[uuid.UUID]) -> Optional[GateSite]:
    """Return the requested active gate, or the estate's primary gate when none is given."""
    if gate_site_id is not None:
        gate = (await db.execute(
            select(GateSite).where(
                GateSite.id == gate_site_id,
                GateSite.app_id == app_id,
                GateSite.is_active.is_(True),
                GateSite.is_deleted.is_(False),
            )
        )).scalar_one_or_none()
        if not gate:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gate site not found or inactive")
        return gate
    gates = await list_gate_sites(db, app_id)
    return gates[0] if gates else None


# --- Threads ---

async def create_thread(db: AsyncSession, user: User, data: ChatThreadCreateSchema) -> Tuple[ChatThread, ChatMessage]:
    if is_guard(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Security officers cannot initiate chat threads. Reply to an existing resident thread instead.",
        )
    await _enforce_rate_limit("chat_thread", user, THREAD_CREATE_RATE_LIMIT)

    gate = await resolve_gate_site(db, user.app_id, data.gate_site_id)
    now = utc_now()
    text = data.initial_message.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message cannot be empty")

    thread = ChatThread(
        app_id=user.app_id,
        resident_id=user.id,
        gate_site_id=gate.id if gate else None,
        unit_address=unit_address(user),
        resident_name=display_name(user),
        subject=(data.subject or ("Emergency" if data.is_emergency else None)),
        status="open",
        has_active_emergency=data.is_emergency,
        last_message_at=now,
        last_message_text=_snippet(text),
        last_sender_role=ROLE_RESIDENT,
        resident_unread_count=0,
        guard_unread_count=1,
    )
    thread.gate_site = gate
    message = ChatMessage(
        thread=thread,
        sender_id=user.id,
        sender_role=ROLE_RESIDENT,
        sender_name=display_name(user),
        is_from_guard=False,
        message=text,
        status_tag="EMERGENCY" if data.is_emergency else None,
        is_read=False,
        created_at=now,
    )
    db.add_all([thread, message])
    await db.commit()
    return thread, message


async def _load_thread(db: AsyncSession, app_id: str, thread_id: uuid.UUID) -> Optional[ChatThread]:
    return (await db.execute(
        select(ChatThread)
        .options(selectinload(ChatThread.gate_site))
        .where(ChatThread.id == thread_id, ChatThread.app_id == app_id)
    )).scalar_one_or_none()


def viewer_role(thread: ChatThread, user: User) -> Optional[str]:
    uid = getattr(user, "id", None)
    if thread.resident_id == uid:
        return ROLE_RESIDENT
    if is_guard(user):
        return ROLE_SECURITY
    return None


async def get_thread_for_user(
    db: AsyncSession,
    user: User,
    thread_id: uuid.UUID,
    *,
    app_id: Optional[str] = None,
    user_id: Optional[uuid.UUID] = None,
    is_user_guard: Optional[bool] = None,
) -> Tuple[ChatThread, str]:
    aid = app_id or str(getattr(user, "app_id", None))
    uid = user_id or getattr(user, "id", None)
    guard = is_guard(user) if is_user_guard is None else is_user_guard

    thread = await _load_thread(db, aid, thread_id)
    if not thread:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat thread not found")

    if thread.resident_id == uid:
        role = ROLE_RESIDENT
    elif guard:
        role = ROLE_SECURITY
    else:
        role = None

    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat thread not found")
    return thread, role


async def drain_chat_message_queue(
    db: AsyncSession,
    batch_size: Optional[int] = None,
) -> int:
    """
    Batch-insert queued chat messages and update corresponding chat threads in Postgres.
    Re-queues on DB failure (at-least-once guarantee).
    """
    size = batch_size or getattr(settings, "CHAT_MESSAGE_DRAIN_BATCH_SIZE", 500)
    dequeued = await dequeue_chat_messages(size)
    if not dequeued:
        return 0

    raw_items = [raw for raw, _ in dequeued]
    try:
        messages: List[ChatMessage] = []
        thread_updates: Dict[uuid.UUID, Dict[str, Any]] = {}

        for _raw, item in dequeued:
            created_at = _parse_at(item.get("created_at"))
            msg_id = _parse_uuid(item.get("id")) or uuid.uuid4()
            thread_id = _parse_uuid(item.get("thread_id"))
            sender_id = _parse_uuid(item.get("sender_id"))
            if not thread_id or not sender_id:
                continue

            msg = ChatMessage(
                id=msg_id,
                thread_id=thread_id,
                sender_id=sender_id,
                sender_role=item["sender_role"],
                sender_name=item["sender_name"],
                officer_badge=item.get("officer_badge"),
                officer_post=item.get("officer_post"),
                is_from_guard=bool(item.get("is_from_guard", False)),
                message=item["message"],
                status_tag=item.get("status_tag"),
                is_read=bool(item.get("is_read", False)),
                created_at=created_at,
                updated_at=created_at,
            )
            messages.append(msg)

            t_up = item.get("thread_update") or {}
            if thread_id not in thread_updates:
                thread_updates[thread_id] = {
                    "last_message_at": created_at,
                    "last_message_text": _snippet(item["message"]),
                    "last_sender_role": item["sender_role"],
                    "inc_resident_unread": 0,
                    "inc_guard_unread": 0,
                    "reopen": False,
                    "emergency": False,
                    "status": None,
                }
            curr = thread_updates[thread_id]
            curr["last_message_at"] = created_at
            curr["last_message_text"] = _snippet(item["message"])
            curr["last_sender_role"] = item["sender_role"]
            curr["inc_resident_unread"] += t_up.get("inc_resident_unread", 0)
            curr["inc_guard_unread"] += t_up.get("inc_guard_unread", 0)
            if t_up.get("reopen_if_resolved"):
                curr["reopen"] = True
            if t_up.get("set_emergency"):
                curr["emergency"] = True
            if t_up.get("status"):
                curr["status"] = t_up.get("status")

        if messages:
            thread_ids = list(thread_updates.keys())
            existing_threads = (await db.execute(
                select(ChatThread.id).where(ChatThread.id.in_(thread_ids))
            )).scalars().all()
            valid_tids = set(existing_threads)

            valid_messages = [m for m in messages if m.thread_id in valid_tids]
            if valid_messages:
                db.add_all(valid_messages)

            for tid, up in thread_updates.items():
                if tid not in valid_tids:
                    continue
                values: Dict[str, Any] = {
                    "last_message_at": up["last_message_at"],
                    "last_message_text": up["last_message_text"],
                    "last_sender_role": up["last_sender_role"],
                }
                stmt = update(ChatThread).where(ChatThread.id == tid)
                if up["inc_resident_unread"]:
                    stmt = stmt.values(resident_unread_count=ChatThread.resident_unread_count + up["inc_resident_unread"])
                if up["inc_guard_unread"]:
                    stmt = stmt.values(guard_unread_count=ChatThread.guard_unread_count + up["inc_guard_unread"])
                if up["status"]:
                    values["status"] = up["status"]
                    if up["status"] == "resolved":
                        values["has_active_emergency"] = False
                elif up["reopen"]:
                    stmt = stmt.values(
                        status=case((ChatThread.status == "resolved", "open"), else_=ChatThread.status)
                    )
                if up["emergency"]:
                    values["has_active_emergency"] = True

                stmt = stmt.values(**values).execution_options(synchronize_session=False)
                await db.execute(stmt)

        await db.commit()
        logger.debug(f"Drained {len(messages)} chat messages from Redis queue")
        return len(messages)
    except Exception as e:
        await db.rollback()
        await requeue_chat_messages(raw_items)
        logger.error(f"Failed to drain chat messages, requeued {len(raw_items)}: {e}", exc_info=True)
        raise


async def list_threads(
    db: AsyncSession,
    user: User,
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[ChatThread]:
    app_id = str(user.app_id)
    user_id = user.id
    user_is_guard = is_guard(user)

    if await chat_message_queue_length() > 0:
        try:
            await drain_chat_message_queue(db)
        except Exception as e:
            logger.debug(f"Inline drain before list_threads error: {e}")

    query = select(ChatThread).options(selectinload(ChatThread.gate_site)).where(ChatThread.app_id == app_id)
    if user_is_guard:
        query = query.order_by(
            ChatThread.has_active_emergency.desc(),
            case((ChatThread.guard_unread_count > 0, 1), else_=0).desc(),
            ChatThread.last_message_at.desc(),
        )
    else:
        query = query.where(ChatThread.resident_id == user_id).order_by(ChatThread.last_message_at.desc())
    if status_filter:
        query = query.where(ChatThread.status == status_filter)
    return (await db.execute(query.limit(limit).offset(offset))).scalars().all()


async def mark_thread_read(db: AsyncSession, thread: ChatThread, role: str, reader_id=None) -> int:
    """Mark the other side's messages as read for `role`. Returns messages updated."""
    incoming_roles = [ROLE_RESIDENT] if role == ROLE_SECURITY else [ROLE_SECURITY, ROLE_SYSTEM]
    result = await db.execute(
        update(ChatMessage)
        .where(
            ChatMessage.thread_id == thread.id,
            ChatMessage.sender_role.in_(incoming_roles),
            ChatMessage.is_read.is_(False),
        )
        .values(is_read=True, read_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if role == ROLE_SECURITY:
        thread.guard_unread_count = 0
    else:
        thread.resident_unread_count = 0
    from app.models.sos import AppNotification
    user_id = reader_id or (thread.resident_id if role != ROLE_SECURITY else None)
    if user_id is not None:
        await db.execute(update(AppNotification).where(AppNotification.user_id==user_id,AppNotification.app_id==thread.app_id,AppNotification.data['thread_id'].as_string()==str(thread.id)).values(is_read=True))
    await db.commit()
    return result.rowcount or 0


async def get_thread_detail(
    db: AsyncSession, user: User, thread_id: uuid.UUID
) -> Tuple[ChatThread, List[ChatMessage], str, int]:
    app_id = str(user.app_id)
    user_id = user.id
    user_is_guard = is_guard(user)

    if await chat_message_queue_length() > 0:
        try:
            await drain_chat_message_queue(db)
        except Exception as e:
            logger.debug(f"Inline drain before get_thread_detail error: {e}")

    thread, role = await get_thread_for_user(
        db, user, thread_id, app_id=app_id, user_id=user_id, is_user_guard=user_is_guard
    )
    marked = await mark_thread_read(db, thread, role, reader_id=user_id)
    messages = (await db.execute(
        select(ChatMessage)
        .where(ChatMessage.thread_id == thread.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
        .execution_options(populate_existing=True)
    )).scalars().all()
    return thread, list(messages), role, marked


async def send_message(
    db: AsyncSession, user: User, thread_id: uuid.UUID, data: ChatMessageCreateSchema
) -> Tuple[ChatThread, ChatMessage, str]:
    await _enforce_rate_limit("chat_message", user, MESSAGE_SEND_RATE_LIMIT)
    thread, role = await get_thread_for_user(db, user, thread_id)
    if thread.status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This chat thread is closed")

    text = data.message.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message cannot be empty")
    status_tag = data.status_tag.strip().upper() if data.status_tag else None
    now = utc_now()
    msg_id = uuid.uuid4()

    if role == ROLE_SECURITY:
        info = await presence_store.get_guard_info(user.app_id, user.id) or {}
        officer_badge = info.get("badge")
        officer_post = info.get("post_name")
        is_from_guard = True
        sender_role = ROLE_SECURITY
        inc_resident = 1
        inc_guard = 0
        thread.resident_unread_count += 1
    else:
        officer_badge = None
        officer_post = None
        is_from_guard = False
        sender_role = ROLE_RESIDENT
        inc_resident = 0
        inc_guard = 1
        thread.guard_unread_count += 1
        if thread.status == "resolved":
            thread.status = "open"
        if status_tag == "EMERGENCY":
            thread.has_active_emergency = True

    thread.last_message_at = now
    thread.last_message_text = _snippet(text)
    thread.last_sender_role = role

    message = ChatMessage(
        id=msg_id,
        thread_id=thread.id,
        sender_id=user.id,
        sender_role=sender_role,
        sender_name=display_name(user),
        officer_badge=officer_badge,
        officer_post=officer_post,
        is_from_guard=is_from_guard,
        message=text,
        status_tag=status_tag,
        is_read=False,
        created_at=now,
    )

    payload = {
        "id": str(msg_id),
        "thread_id": str(thread.id),
        "app_id": thread.app_id,
        "sender_id": str(user.id),
        "sender_role": sender_role,
        "sender_name": display_name(user),
        "officer_badge": officer_badge,
        "officer_post": officer_post,
        "is_from_guard": is_from_guard,
        "message": text,
        "status_tag": status_tag,
        "created_at": now.isoformat(),
        "thread_update": {
            "thread_id": str(thread.id),
            "app_id": thread.app_id,
            "inc_resident_unread": inc_resident,
            "inc_guard_unread": inc_guard,
            "reopen_if_resolved": role == ROLE_RESIDENT,
            "set_emergency": status_tag == "EMERGENCY" if role == ROLE_RESIDENT else False,
            "status": None,
        },
    }

    queued = await enqueue_chat_message(payload)
    if not queued:
        db.add(message)
        await db.commit()

    return thread, message, role


async def resolve_thread(db: AsyncSession, user: User, thread_id: uuid.UUID) -> Tuple[ChatThread, ChatMessage, str]:
    thread, role = await get_thread_for_user(db, user, thread_id)
    if thread.status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This chat thread is closed")

    now = utc_now()
    resolver = display_name(user)
    note = f"Marked as resolved by {resolver}"
    officer_badge = None
    officer_post = None
    if role == ROLE_SECURITY:
        info = await presence_store.get_guard_info(user.app_id, user.id) or {}
        officer_badge = info.get("badge")
        officer_post = info.get("post_name")
        if officer_post:
            note += f" ({officer_post})"

    msg_id = uuid.uuid4()
    message = ChatMessage(
        id=msg_id,
        thread_id=thread.id,
        sender_id=user.id,
        sender_role=ROLE_SYSTEM,
        sender_name=resolver,
        officer_badge=officer_badge,
        officer_post=officer_post,
        is_from_guard=role == ROLE_SECURITY,
        message=note,
        status_tag="RESOLVED",
        is_read=False,
        created_at=now,
    )
    thread.status = "resolved"
    thread.has_active_emergency = False
    thread.last_message_at = now
    thread.last_message_text = note
    thread.last_sender_role = ROLE_SYSTEM

    payload = {
        "id": str(msg_id),
        "thread_id": str(thread.id),
        "app_id": thread.app_id,
        "sender_id": str(user.id),
        "sender_role": ROLE_SYSTEM,
        "sender_name": resolver,
        "officer_badge": officer_badge,
        "officer_post": officer_post,
        "is_from_guard": role == ROLE_SECURITY,
        "message": note,
        "status_tag": "RESOLVED",
        "created_at": now.isoformat(),
        "thread_update": {
            "thread_id": str(thread.id),
            "app_id": thread.app_id,
            "inc_resident_unread": 0,
            "inc_guard_unread": 0,
            "reopen_if_resolved": False,
            "set_emergency": False,
            "status": "resolved",
        },
    }

    queued = await enqueue_chat_message(payload)
    if not queued:
        db.add(message)
        await db.commit()

    return thread, message, role


# --- Guard Duty ---

async def update_guard_duty(db: AsyncSession, user: User, data: GuardDutyUpdateSchema) -> GuardPresenceSchema:
    if not is_guard(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only security officers can set duty status")

    previous = await presence_store.get_guard_info(user.app_id, user.id) or {}
    if data.gate_site_id is not None:
        gate = await resolve_gate_site(db, user.app_id, data.gate_site_id)
    elif previous.get("post_id"):
        try:
            gate = await resolve_gate_site(db, user.app_id, uuid.UUID(previous["post_id"]))
        except HTTPException:
            gate = await resolve_gate_site(db, user.app_id, None)
    else:
        gate = await resolve_gate_site(db, user.app_id, None)

    now = utc_now()
    info: Dict[str, Any] = {
        "guard_id": str(user.id),
        "name": display_name(user),
        "status": data.status,
        "badge": data.badge or previous.get("badge"),
        "post_id": str(gate.id) if gate else None,
        "post_name": gate.name if gate else None,
        "updated_at": now.isoformat(),
    }
    if data.status == "off_duty":
        await presence_store.remove_guard_duty(user.app_id, user.id, keep_info=info)
    else:
        await presence_store.set_guard_duty(user.app_id, user.id, info)
    return GuardPresenceSchema(**info)


async def list_active_guards(app_id: str) -> List[GuardPresenceSchema]:
    guards = await presence_store.list_guards_on_duty(app_id)
    result = [GuardPresenceSchema(**g) for g in guards if g.get("status") in ROUTABLE_DUTY_STATUSES]
    return sorted(result, key=lambda g: ((g.post_name or ""), (g.name or "")))


async def route_guard_ids(db: AsyncSession, app_id: str, gate_site_id: Optional[uuid.UUID]) -> List[str]:
    """Guards who should receive a resident message for this gate."""
    on_duty = [g for g in await presence_store.list_guards_on_duty(app_id) if g.get("status") in ROUTABLE_DUTY_STATUSES]
    at_gate = [g for g in on_duty if gate_site_id is not None and g.get("post_id") == str(gate_site_id)]
    targets = at_gate or on_duty
    if targets:
        return [g["guard_id"] for g in targets]

    rows = (await db.execute(
        select(User.id).where(
            User.app_id == app_id,
            User.roles.contains([UserRole.SECURITY]),
            User.is_revoked.is_(False),
        )
    )).scalars().all()
    return [str(r) for r in rows]


# --- Dispatch (WebSocket + FCM) ---

@dataclass
class PushTarget:
    user_id: str
    token: str
    notification_count: int | None = None


@dataclass
class ChatDispatch:
    app_id: str
    thread_id: str
    event: Dict[str, Any]
    push_targets: List[PushTarget] = field(default_factory=list)
    title: str = ""
    body: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    high_priority: bool = True


@dataclass
class PushReport:
    sent: List[str] = field(default_factory=list)
    suppressed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    stale_tokens: List[str] = field(default_factory=list)


async def prepare_dispatch(
    db: AsyncSession,
    *,
    event_type: str,
    thread: ChatThread,
    message: ChatMessage,
    actor: User,
    actor_role: str,
) -> ChatDispatch:
    """
    Resolve recipients and FCM tokens while the request's DB session is still open, so the
    actual sends can run in a background task.
    """
    guard_ids = await route_guard_ids(db, thread.app_id, thread.gate_site_id)
    resident_id = str(thread.resident_id)
    actor_id = str(actor.id)

    recipients = list(dict.fromkeys([*guard_ids, resident_id, actor_id]))
    if actor_role == ROLE_RESIDENT:
        push_ids = guard_ids
    else:
        # Guard replies and resolutions notify the resident; other guards get the WS sync only.
        push_ids = [resident_id]
    push_ids = [uid for uid in dict.fromkeys(push_ids) if uid != actor_id]

    push_targets: List[PushTarget] = []
    if push_ids:
        eligible = (
            User.id.in_([uuid.UUID(uid) for uid in push_ids]),
            User.app_id == thread.app_id,
            User.is_revoked.is_(False),
        )
        devices = (await db.execute(
            select(User.id, PushDevice.token).join(PushDevice, PushDevice.user_id == User.id)
            .where(*eligible)
        )).all()
        legacy = (await db.execute(
            select(User.id, User.fcm_token).where(*eligible, User.fcm_token.is_not(None))
        )).all()
        # Prefer the device registry; retain legacy accounts until their next token sync.
        tokens = {token: str(uid) for uid, token in [*legacy, *devices] if token}
        push_targets = [PushTarget(uid, token) for token, uid in tokens.items()]

    thread_payload = thread_to_schema(thread).model_dump(mode="json")
    message_payload = message_to_schema(message).model_dump(mode="json")
    gate_name = thread_payload.get("gate_site_name")
    snippet = _snippet(message.message)

    if actor_role == ROLE_RESIDENT:
        title = "🚨 EMERGENCY • " + (gate_name or "Gatehouse") if thread.has_active_emergency else (gate_name or "Gatehouse chat")
        where = thread.unit_address or thread.resident_name
        body = f"{snippet} • {where}" if where else snippet
    elif message.sender_role == ROLE_SYSTEM:
        title = "Gatehouse chat resolved"
        body = snippet
    else:
        title = message.sender_name + (f" • {message.officer_post}" if message.officer_post else "")
        body = snippet

    from app.services.notification_inbox import record, unread_count
    for uid in push_ids:
        eligible_user=(await db.execute(select(User).where(User.id==uuid.UUID(uid),User.app_id==thread.app_id,User.is_revoked.is_(False)))).scalar_one_or_none()
        if eligible_user and await presence_store.get_active_room(str(eligible_user.id)) != str(thread.id):
            await record(db,eligible_user.id,thread.app_id,'chat:'+str(message.id),title,body,{'type':'chat_message','thread_id':str(thread.id),'message_id':str(message.id),'app_id':thread.app_id})
    for target in push_targets:
        target.notification_count = await unread_count(db,uuid.UUID(target.user_id),thread.app_id)
    await db.commit()
    return ChatDispatch(
        app_id=thread.app_id,
        thread_id=str(thread.id),
        event={
            "event": event_type,
            "thread_id": str(thread.id),
            "sender_id": actor_id,
            "recipients": recipients,
            "thread": thread_payload,
            "message": message_payload,
        },
        push_targets=push_targets,
        title=title,
        body=body,
        data={
            "type": "chat_message",
            "event": event_type,
            "thread_id": str(thread.id),
            "message_id": str(message.id),
            "app_id": thread.app_id,
            "sender_role": message.sender_role,
            "is_emergency": thread.has_active_emergency,
            "deep_link": f"residence://chat/threads/{thread.id}",
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
        },
        high_priority=thread.has_active_emergency or actor_role == ROLE_RESIDENT or message.sender_role == ROLE_SECURITY,
    )


async def broadcast(dispatch: ChatDispatch) -> int:
    return await chat_hub.publish(dispatch.app_id, dispatch.event)


async def send_push_notifications(dispatch: ChatDispatch) -> PushReport:
    """FCM fallback: push only to recipients who are not currently viewing this thread."""
    report = PushReport()
    for target in dispatch.push_targets:
        if await presence_store.get_active_room(target.user_id) == dispatch.thread_id:
            report.suppressed.append(target.user_id)
            continue
        result = await fcm_service.send_to_token(
            target.token, dispatch.title, dispatch.body, {**dispatch.data, **({"notification_count": target.notification_count} if target.notification_count is not None else {})}, dispatch.high_priority
        )
        if result.ok:
            report.sent.append(target.user_id)
        else:
            report.failed.append(target.user_id)
            if result.status == PushStatus.UNREGISTERED:
                report.stale_tokens.append(target.user_id)
    if report.stale_tokens:
        logger.warning(f"FCM tokens no longer registered for users: {report.stale_tokens}")
    return report


async def dispatch(dispatch_plan: ChatDispatch) -> PushReport:
    await broadcast(dispatch_plan)
    return await send_push_notifications(dispatch_plan)


async def list_audit_threads(
    db: AsyncSession, user: User, *, search: Optional[str] = None,
    status_filter: Optional[str] = None, limit: int = 50, offset: int = 0,
) -> Tuple[Sequence[ChatThread], bool]:
    """Caretaker's estate-wide archive; never changes participant read state."""
    if UserRole.CARETAKER not in (user.roles or []):
        raise HTTPException(status_code=403, detail="Estate caretaker access required")
    if await chat_message_queue_length() > 0:
        await drain_chat_message_queue(db)
    query = select(ChatThread).options(selectinload(ChatThread.gate_site)).where(
        ChatThread.app_id == user.app_id
    )
    if status_filter:
        query = query.where(ChatThread.status == status_filter)
    if search and search.strip():
        from sqlalchemy import or_
        # Escape LIKE metacharacters so the search is a literal substring.
        term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        message_match = select(ChatMessage.id).where(
            ChatMessage.thread_id == ChatThread.id,
            or_(ChatMessage.message.ilike(pattern, escape="\\"),
                ChatMessage.sender_name.ilike(pattern, escape="\\"),
                ChatMessage.officer_badge.ilike(pattern, escape="\\"),
                ChatMessage.officer_post.ilike(pattern, escape="\\")),
        ).exists()
        query = query.where(or_(
            ChatThread.resident_name.ilike(pattern, escape="\\"),
            ChatThread.unit_address.ilike(pattern, escape="\\"),
            ChatThread.subject.ilike(pattern, escape="\\"), message_match,
        ))
    rows = (await db.execute(query.order_by(
        ChatThread.last_message_at.desc(), ChatThread.id.desc()
    ).offset(offset).limit(limit + 1))).scalars().all()
    return rows[:limit], len(rows) > limit


async def get_audit_thread(
    db: AsyncSession, user: User, thread_id: uuid.UUID,
) -> Tuple[ChatThread, List[ChatMessage]]:
    if UserRole.CARETAKER not in (user.roles or []):
        raise HTTPException(status_code=403, detail="Estate caretaker access required")
    if await chat_message_queue_length() > 0:
        await drain_chat_message_queue(db)
    thread = await _load_thread(db, user.app_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat thread not found")
    messages = (await db.execute(select(ChatMessage).where(
        ChatMessage.thread_id == thread.id
    ).order_by(ChatMessage.created_at, ChatMessage.id))).scalars().all()
    return thread, list(messages)
