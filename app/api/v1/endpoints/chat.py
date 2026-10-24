import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from jose import JWTError, jwt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import is_token_blacklisted
from app.dependencies import check_roles, get_current_user
from app.models.user import User, UserRole
from app.models.estate import Estate
from app.schemas.chat import (
    ChatAuditPageSchema,
    ChatMessageCreateSchema,
    ChatMessageSchema,
    ChatThreadCreateSchema,
    ChatThreadDetailSchema,
    ChatThreadSchema,
    GateSiteCreateSchema,
    GateSiteSchema,
    GuardDutyUpdateSchema,
    GuardPresenceSchema,
    ThreadStatus,
)
from app.services import chat as chat_service
from app.services.chat_websocket import ChatConnection, chat_hub

logger = logging.getLogger(__name__)

router = APIRouter()

WS_CLOSE_UNAUTHORIZED = 4401


def _detail_schema(thread, messages) -> ChatThreadDetailSchema:
    base = chat_service.thread_to_schema(thread).model_dump()
    return ChatThreadDetailSchema(**base, messages=[chat_service.message_to_schema(m) for m in messages])


# --- Gate Sites ---

@router.get("/gates", response_model=List[GateSiteSchema])
async def list_gates(
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Active gate sites for the caller's estate (drives the resident's gate selector)."""
    if include_inactive and UserRole.CARETAKER not in current_user.roles:
        raise HTTPException(status_code=403, detail="Estate caretaker access required")
    return await chat_service.list_gate_sites(db, current_user.app_id, include_inactive)


@router.post("/gates", response_model=GateSiteSchema, status_code=status.HTTP_201_CREATED)
async def create_gate(
    gate_in: GateSiteCreateSchema,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER])),
):
    """Define a new gate site (estate admin / caretaker only)."""
    return await chat_service.create_gate_site(db, current_user.app_id, gate_in)


# --- Threads ---

@router.post("/threads", response_model=ChatThreadDetailSchema, status_code=status.HTTP_201_CREATED)
async def create_thread(
    thread_in: ChatThreadCreateSchema,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Resident opens a chat with a gate site. Security officers are rejected with 403.
    The message is broadcast to on-duty guards; guards not viewing the thread get an FCM push.
    """
    thread, message = await chat_service.create_thread(db, current_user, thread_in)
    plan = await chat_service.prepare_dispatch(
        db, event_type="thread_created", thread=thread, message=message,
        actor=current_user, actor_role=chat_service.ROLE_RESIDENT,
    )
    await chat_service.broadcast(plan)
    background_tasks.add_task(chat_service.send_push_notifications, plan)
    return _detail_schema(thread, [message])


@router.get("/threads", response_model=List[ChatThreadSchema])
async def list_threads(
    status_filter: Optional[ThreadStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Residents: their own threads, newest first.
    Guards: every estate thread — emergencies first, then unread, then most recent.
    """
    threads = await chat_service.list_threads(db, current_user, status_filter, limit, offset)
    return [chat_service.thread_to_schema(t) for t in threads]


@router.get("/threads/{thread_id}", response_model=ChatThreadDetailSchema)
async def get_thread(
    thread_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Thread metadata and full history. Marks the other side's messages as read."""
    user_id_str = str(current_user.id)
    thread, messages, role, marked = await chat_service.get_thread_detail(db, current_user, thread_id)
    if marked:
        thread_payload = chat_service.thread_to_schema(thread).model_dump(mode="json")
        background_tasks.add_task(
            _publish_read_receipt,
            str(thread.app_id),
            str(thread.id),
            str(thread.resident_id),
            role,
            user_id_str,
            thread_payload,
        )
    return _detail_schema(thread, messages)


@router.post("/threads/{thread_id}/resolve", response_model=ChatThreadSchema)
async def resolve_thread(
    thread_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    thread, message, role = await chat_service.resolve_thread(db, current_user, thread_id)
    plan = await chat_service.prepare_dispatch(
        db, event_type="thread_resolved", thread=thread, message=message,
        actor=current_user, actor_role=role,
    )
    await chat_service.broadcast(plan)
    background_tasks.add_task(chat_service.send_push_notifications, plan)
    return chat_service.thread_to_schema(thread)


# --- Messages ---

@router.post("/threads/{thread_id}/messages", response_model=ChatMessageSchema, status_code=status.HTTP_201_CREATED)
async def send_message(
    thread_id: uuid.UUID,
    message_in: ChatMessageCreateSchema,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resident or guard message. Guard messages are stamped with officer name, badge and post."""
    thread, message, role = await chat_service.send_message(db, current_user, thread_id, message_in)
    plan = await chat_service.prepare_dispatch(
        db, event_type="new_message", thread=thread, message=message,
        actor=current_user, actor_role=role,
    )
    await chat_service.broadcast(plan)
    background_tasks.add_task(chat_service.send_push_notifications, plan)
    return chat_service.message_to_schema(message)


# --- Guard Duty & Presence ---

@router.post("/guard/duty", response_model=GuardPresenceSchema)
async def set_guard_duty(
    duty_in: GuardDutyUpdateSchema,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.SECURITY])),
):
    """Officer sets on_duty / on_break / off_duty and the gate post they are working."""
    return await chat_service.update_guard_duty(db, current_user, duty_in)


@router.get("/guard/active", response_model=List[GuardPresenceSchema])
async def list_active_guards(
    current_user: User = Depends(check_roles([UserRole.SECURITY, UserRole.CARETAKER])),
):
    return await chat_service.list_active_guards(current_user.app_id)


async def _publish_read_receipt(
    app_id: str,
    thread_id: str,
    resident_id: str,
    role: str,
    reader_id: str,
    thread_payload: dict,
) -> None:
    recipients = [resident_id, reader_id]
    if role == chat_service.ROLE_RESIDENT:
        guards = await chat_service.list_active_guards(app_id)
        recipients += [str(g.guard_id) for g in guards]
    await chat_hub.publish(app_id, {
        "event": "thread_read",
        "thread_id": thread_id,
        "sender_id": reader_id,
        "reader_role": role,
        "recipients": recipients,
        "thread": thread_payload,
    })


# --- WebSocket ---

@asynccontextmanager
async def _ws_session(websocket: WebSocket) -> AsyncIterator[AsyncSession]:
    """
    Short-lived DB session per WebSocket action, so an idle socket never pins a pooled
    connection. Resolves get_db through dependency_overrides so tests hit the test DB.
    """
    provider = websocket.app.dependency_overrides.get(get_db, get_db)
    agen = provider()
    session = await agen.__anext__()
    try:
        yield session
    finally:
        await agen.aclose()


async def _authenticate_ws(websocket: WebSocket, token: str) -> Optional[User]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    email, app_id = payload.get("sub"), payload.get("app_id")
    if not email or not app_id or await is_token_blacklisted(token):
        return None
    async with _ws_session(websocket) as db:
        user = (await db.execute(
            select(User).join(Estate, User.app_id == Estate.app_id).where(
                User.email == email, User.app_id == app_id, Estate.is_active.is_(True)
            )
        )).scalar_one_or_none()
    if not user or user.is_revoked or user.must_change_password or payload.get("purpose") == "password_change":
        return None
    return user


async def _ws_send_message(websocket: WebSocket, user: User, data: dict) -> dict:
    thread_id = uuid.UUID(str(data.get("thread_id")))
    message_in = ChatMessageCreateSchema(message=data.get("text") or data.get("message") or "", status_tag=data.get("status_tag"))
    async with _ws_session(websocket) as db:
        thread, message, role = await chat_service.send_message(db, user, thread_id, message_in)
        plan = await chat_service.prepare_dispatch(
            db, event_type="new_message", thread=thread, message=message, actor=user, actor_role=role,
        )
    await chat_service.broadcast(plan)
    chat_hub.spawn(chat_service.send_push_notifications(plan))
    return {
        "action": "message_sent",
        "thread_id": str(thread_id),
        "client_id": data.get("client_id"),
        "message": chat_service.message_to_schema(message).model_dump(mode="json"),
    }


@router.websocket("/ws")
async def chat_websocket(websocket: WebSocket, token: str = Query(...)):
    """
    Real-time channel. Client events:
      {"action": "join_room", "thread_id": "..."}   -> marks presence (suppresses FCM for that thread)
      {"action": "leave_room", "thread_id": "..."}
      {"action": "send_message", "thread_id": "...", "text": "...", "status_tag": "..."}
      {"action": "ping"}                            -> {"action": "pong"}, renews presence TTL
    """
    user = await _authenticate_ws(websocket, token)
    if user is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept()
    conn = ChatConnection(
        websocket=websocket,
        user_id=str(user.id),
        app_id=user.app_id,
        roles={r.value for r in user.roles},
    )
    await chat_hub.connect(conn)
    await conn.send_json({"action": "connected", "user_id": str(user.id), "is_guard": chat_service.is_guard(user)})

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except (ValueError, KeyError):
                await conn.send_json({"action": "error", "detail": "Invalid JSON payload"})
                continue
            if not isinstance(data, dict):
                await conn.send_json({"action": "error", "detail": "Payload must be a JSON object"})
                continue

            action = data.get("action")
            try:
                if action == "ping":
                    await chat_hub.heartbeat(conn)
                    await conn.send_json({"action": "pong"})

                elif action == "join_room":
                    thread_id = uuid.UUID(str(data.get("thread_id")))
                    async with _ws_session(websocket) as db:
                        thread, role = await chat_service.get_thread_for_user(db, user, thread_id)
                        marked = await chat_service.mark_thread_read(db, thread, role)
                    await chat_hub.join_room(conn, thread_id)
                    await conn.send_json({"action": "room_joined", "thread_id": str(thread_id)})
                    if marked:
                        thread_payload = chat_service.thread_to_schema(thread).model_dump(mode="json")
                        await _publish_read_receipt(
                            str(thread.app_id),
                            str(thread.id),
                            str(thread.resident_id),
                            role,
                            str(user.id),
                            thread_payload,
                        )

                elif action == "leave_room":
                    await chat_hub.leave_room(conn, data.get("thread_id"))
                    await conn.send_json({"action": "room_left", "thread_id": data.get("thread_id")})

                elif action == "send_message":
                    await conn.send_json(await _ws_send_message(websocket, user, data))

                else:
                    await conn.send_json({"action": "error", "detail": f"Unknown action: {action}"})

            except HTTPException as e:
                await conn.send_json({"action": "error", "request": action, "status_code": e.status_code, "detail": e.detail})
            except (ValueError, ValidationError) as e:
                await conn.send_json({"action": "error", "request": action, "status_code": 422, "detail": str(e)})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception(f"Chat WebSocket error for user {user.id}: {e}")
    finally:
        await chat_hub.disconnect(conn)


# Separate audit endpoints keep caretaker access out of the participant APIs.
@router.get("/audit/threads", response_model=ChatAuditPageSchema)
async def list_conversation_logs(
    search: Optional[str] = Query(None, max_length=200),
    status_filter: Optional[ThreadStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER])),
):
    threads, has_more = await chat_service.list_audit_threads(
        db, current_user, search=search, status_filter=status_filter, limit=limit, offset=offset,
    )
    return ChatAuditPageSchema(
        threads=[chat_service.thread_to_schema(t) for t in threads], has_more=has_more,
    )


@router.get("/audit/threads/{thread_id}", response_model=ChatThreadDetailSchema)
async def get_conversation_log(
    thread_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER])),
):
    thread, messages = await chat_service.get_audit_thread(db, current_user, thread_id)
    return _detail_schema(thread, messages)


from pydantic import BaseModel
class GateStatusUpdate(BaseModel):
    is_active: bool

async def _managed_gate(db, user, gate_id):
    from app.models.chat import GateSite
    gate = (await db.execute(select(GateSite).where(GateSite.id == gate_id, GateSite.app_id == user.app_id, GateSite.is_deleted.is_(False)))).scalar_one_or_none()
    if not gate:
        raise HTTPException(status_code=404, detail="Gate site not found")
    return gate

@router.patch("/gates/{gate_id}", response_model=GateSiteSchema)
async def update_gate_status(gate_id: uuid.UUID, data: GateStatusUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(check_roles([UserRole.CARETAKER]))):
    gate = await _managed_gate(db, user, gate_id)
    gate.is_active = data.is_active
    await db.commit()
    await db.refresh(gate)
    return gate

@router.delete("/gates/{gate_id}", status_code=204)
async def delete_gate(gate_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(check_roles([UserRole.CARETAKER]))):
    gate = await _managed_gate(db, user, gate_id)
    gate.is_active = False
    gate.is_deleted = True
    await db.commit()
