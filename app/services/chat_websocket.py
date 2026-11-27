"""
Real-time chat delivery: WebSocket connection registry, room presence, guard duty
roster, and Redis Pub/Sub fan-out across API workers.

Redis keys (see Gatehouse chat spec §4):
    residence:chat:active_room:{user_id}        -> thread_id the user is viewing (TTL 120s, heartbeat-renewed)
    residence:guard:duty:{app_id}               -> SET of guard user_ids currently on duty / on break
    residence:guard:info:{app_id}:{guard_id}    -> JSON guard details (post, badge, status), TTL 24h
    residence:chat:pubsub:{app_id}              -> Pub/Sub channel for inter-worker distribution

Every Redis operation degrades to an in-process store when Redis is unreachable so a
single-worker deployment (and local dev) keeps working.
"""
import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, Iterable, List, Optional, Set

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

ACTIVE_ROOM_TTL_SECONDS = 120
GUARD_INFO_TTL_SECONDS = 86400
PUBSUB_PATTERN = "residence:chat:pubsub:*"

# Delete the presence key only if it still points at the room being left, so a stale
# leave from one device can't clear a newer join from another.
_CLEAR_IF_MATCHES_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def active_room_key(user_id: Any) -> str:
    return f"residence:chat:active_room:{user_id}"


def guard_duty_key(app_id: str) -> str:
    return f"residence:guard:duty:{app_id}"


def guard_info_key(app_id: str, guard_id: Any) -> str:
    return f"residence:guard:info:{app_id}:{guard_id}"


def pubsub_channel(app_id: str) -> str:
    return f"residence:chat:pubsub:{app_id}"


class PresenceStore:
    """Room presence and guard duty roster. Redis is the shared source of truth."""

    def __init__(self) -> None:
        self._rooms: Dict[str, tuple[str, float]] = {}
        self._duty: Dict[str, Set[str]] = defaultdict(set)
        self._guard_info: Dict[tuple[str, str], Dict[str, Any]] = {}

    # --- Room presence ---

    async def set_active_room(self, user_id: Any, thread_id: Any) -> None:
        uid, tid = str(user_id), str(thread_id)
        self._rooms[uid] = (tid, time.monotonic() + ACTIVE_ROOM_TTL_SECONDS)
        client = get_redis_client()
        if not client:
            return
        try:
            await client.set(active_room_key(uid), tid, ex=ACTIVE_ROOM_TTL_SECONDS)
        except Exception as e:
            logger.debug(f"Redis set_active_room error: {e}")

    async def clear_active_room(self, user_id: Any, thread_id: Optional[Any] = None) -> None:
        uid = str(user_id)
        local = self._rooms.get(uid)
        if local and (thread_id is None or local[0] == str(thread_id)):
            self._rooms.pop(uid, None)
        client = get_redis_client()
        if not client:
            return
        try:
            if thread_id is None:
                await client.delete(active_room_key(uid))
            else:
                await client.eval(_CLEAR_IF_MATCHES_LUA, 1, active_room_key(uid), str(thread_id))
        except Exception as e:
            logger.debug(f"Redis clear_active_room error: {e}")

    async def get_active_room(self, user_id: Any) -> Optional[str]:
        uid = str(user_id)
        client = get_redis_client()
        if client:
            try:
                return await client.get(active_room_key(uid))
            except Exception as e:
                logger.debug(f"Redis get_active_room error: {e}")
        local = self._rooms.get(uid)
        if local and local[1] > time.monotonic():
            return local[0]
        self._rooms.pop(uid, None)
        return None

    # --- Guard duty roster ---

    async def set_guard_duty(self, app_id: str, guard_id: Any, info: Dict[str, Any]) -> None:
        gid = str(guard_id)
        self._duty[app_id].add(gid)
        self._guard_info[(app_id, gid)] = info
        client = get_redis_client()
        if not client:
            return
        try:
            await client.sadd(guard_duty_key(app_id), gid)
            await client.set(guard_info_key(app_id, gid), json.dumps(info, default=str), ex=GUARD_INFO_TTL_SECONDS)
        except Exception as e:
            logger.debug(f"Redis set_guard_duty error: {e}")

    async def remove_guard_duty(self, app_id: str, guard_id: Any, keep_info: Optional[Dict[str, Any]] = None) -> None:
        """Take a guard off the roster. `keep_info` preserves badge/post for later reply tagging."""
        gid = str(guard_id)
        self._duty[app_id].discard(gid)
        if keep_info is None:
            self._guard_info.pop((app_id, gid), None)
        else:
            self._guard_info[(app_id, gid)] = keep_info
        client = get_redis_client()
        if not client:
            return
        try:
            await client.srem(guard_duty_key(app_id), gid)
            if keep_info is None:
                await client.delete(guard_info_key(app_id, gid))
            else:
                await client.set(guard_info_key(app_id, gid), json.dumps(keep_info, default=str), ex=GUARD_INFO_TTL_SECONDS)
        except Exception as e:
            logger.debug(f"Redis remove_guard_duty error: {e}")

    async def get_guard_info(self, app_id: str, guard_id: Any) -> Optional[Dict[str, Any]]:
        gid = str(guard_id)
        client = get_redis_client()
        if client:
            try:
                raw = await client.get(guard_info_key(app_id, gid))
                return json.loads(raw) if raw else None
            except Exception as e:
                logger.debug(f"Redis get_guard_info error: {e}")
        return self._guard_info.get((app_id, gid))

    async def list_guards_on_duty(self, app_id: str) -> List[Dict[str, Any]]:
        client = get_redis_client()
        if client:
            try:
                guard_ids = await client.smembers(guard_duty_key(app_id))
                guards: List[Dict[str, Any]] = []
                stale: List[str] = []
                for gid in guard_ids or []:
                    raw = await client.get(guard_info_key(app_id, gid))
                    if raw:
                        guards.append(json.loads(raw))
                    else:
                        stale.append(gid)
                if stale:
                    await client.srem(guard_duty_key(app_id), *stale)
                return guards
            except Exception as e:
                logger.debug(f"Redis list_guards_on_duty error: {e}")
        return [
            self._guard_info[(app_id, gid)]
            for gid in self._duty.get(app_id, set())
            if (app_id, gid) in self._guard_info
        ]


@dataclass(eq=False)
class ChatConnection:
    """One live WebSocket for the active account session."""

    websocket: Any
    user_id: str
    app_id: str
    roles: Set[str] = field(default_factory=set)
    authorize: Optional[Callable[[], Awaitable[bool]]] = None
    room: Optional[str] = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_json(self, payload: Dict[str, Any]) -> bool:
        async with self._send_lock:
            try:
                if self.authorize and not await self.authorize():
                    await self.websocket.close(code=4401)
                    return False
                await self.websocket.send_json(payload)
                return True
            except Exception as e:
                logger.debug(f"WebSocket send failed for user {self.user_id}: {e}")
                return False


class ChatHub:
    """
    Registry of this worker's sockets plus Redis Pub/Sub fan-out.

    `publish()` delivers to local sockets immediately and broadcasts the event; every
    other worker's listener delivers it to the sockets it holds. Events carry the
    origin worker id so the publisher never delivers its own event twice.
    """

    def __init__(self, presence: PresenceStore) -> None:
        self.presence = presence
        self.worker_id = uuid.uuid4().hex
        self._connections: Dict[str, Set[ChatConnection]] = defaultdict(set)
        self._listener_task: Optional[asyncio.Task] = None
        self._background: Set[asyncio.Task] = set()
        self._running = False

    # --- Connection lifecycle ---

    async def connect(self, conn: ChatConnection) -> None:
        self._connections[conn.user_id].add(conn)

    async def disconnect(self, conn: ChatConnection) -> None:
        conns = self._connections.get(conn.user_id)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                self._connections.pop(conn.user_id, None)
        if conn.room and not self._user_in_room_locally(conn.user_id, conn.room):
            await self.presence.clear_active_room(conn.user_id, conn.room)
        conn.room = None

    async def join_room(self, conn: ChatConnection, thread_id: Any) -> None:
        previous = conn.room
        conn.room = str(thread_id)
        if previous and previous != conn.room and not self._user_in_room_locally(conn.user_id, previous):
            await self.presence.clear_active_room(conn.user_id, previous)
        await self.presence.set_active_room(conn.user_id, conn.room)

    async def leave_room(self, conn: ChatConnection, thread_id: Optional[Any] = None) -> None:
        room = conn.room
        if room is None or (thread_id is not None and str(thread_id) != room):
            return
        conn.room = None
        if not self._user_in_room_locally(conn.user_id, room):
            await self.presence.clear_active_room(conn.user_id, room)

    async def heartbeat(self, conn: ChatConnection) -> None:
        if conn.room:
            await self.presence.set_active_room(conn.user_id, conn.room)

    def _user_in_room_locally(self, user_id: str, room: str) -> bool:
        return any(c.room == room for c in self._connections.get(user_id, ()))

    def local_connection_count(self, user_id: Optional[Any] = None) -> int:
        if user_id is not None:
            return len(self._connections.get(str(user_id), ()))
        return sum(len(c) for c in self._connections.values())

    # --- Event fan-out ---

    async def publish(self, app_id: str, event: Dict[str, Any]) -> int:
        """Deliver locally, then broadcast to other workers. Returns local deliveries."""
        event = {**event, "app_id": app_id, "origin": self.worker_id}
        delivered = await self.deliver_local(event)
        client = get_redis_client()
        if client:
            try:
                await client.publish(pubsub_channel(app_id), json.dumps(event, default=str))
            except Exception as e:
                logger.debug(f"Redis chat publish error: {e}")
        return delivered

    async def deliver_local(self, event: Dict[str, Any]) -> int:
        recipients: Iterable[str] = event.get("recipients") or []
        thread_id = event.get("thread_id")
        sender_id = event.get("sender_id")
        payload = {k: v for k, v in event.items() if k not in ("recipients", "origin")}

        delivered = 0
        for uid in dict.fromkeys(str(r) for r in recipients):
            for conn in list(self._connections.get(uid, ())):
                if conn.app_id != event.get("app_id"):
                    continue
                in_room = thread_id is not None and conn.room == str(thread_id)
                ok = await conn.send_json({
                    **payload,
                    "in_room": in_room,
                    "play_chime": in_room and uid != str(sender_id),
                })
                delivered += int(ok)
        return delivered

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Run fire-and-forget work (push dispatch) without letting the task be GC'd."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # --- Pub/Sub listener ---

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if get_redis_client():
            self._listener_task = asyncio.create_task(self._listen(), name="chat-pubsub-listener")

    async def stop(self) -> None:
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
            self._listener_task = None
        for task in list(self._background):
            task.cancel()

    async def _listen(self) -> None:
        backoff = 1.0
        while self._running:
            client = get_redis_client()
            if not client:
                return
            pubsub = client.pubsub()
            try:
                await pubsub.psubscribe(PUBSUB_PATTERN)
                backoff = 1.0
                while self._running:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message or message.get("type") != "pmessage":
                        continue
                    try:
                        event = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if event.get("origin") == self.worker_id:
                        continue
                    await self.deliver_local(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Chat Pub/Sub listener error, reconnecting in {backoff:.0f}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    await pubsub.aclose() if hasattr(pubsub, "aclose") else await pubsub.close()
                except Exception:
                    pass


presence_store = PresenceStore()
chat_hub = ChatHub(presence_store)
