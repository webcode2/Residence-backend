import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import select

from app.api.v1.endpoints.chat import WS_CLOSE_UNAUTHORIZED, chat_websocket
from app.main import app
from app.models.chat import ChatMessage, ChatThread
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.core.redis import get_redis_client, CHAT_MESSAGE_QUEUE_KEY
from app.services.auth import create_access_token
from app.services.chat_websocket import ChatConnection, chat_hub, presence_store
from app.services.fcm import PushResult, PushStatus


# --- Fixtures & helpers ---

class FakeSocket:
    """Captures events pushed by the ChatHub."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.events.append(payload)

    def of(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("event") == event]


class FakeWebSocket(FakeSocket):
    """Drives the /ws handler with a scripted list of client frames."""

    def __init__(self, frames: List[Any]) -> None:
        super().__init__()
        self.app = app
        self.frames = list(frames)
        self.accepted = False
        self.close_code = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    async def receive_json(self) -> Any:
        if not self.frames:
            raise WebSocketDisconnect(code=1000)
        return self.frames.pop(0)

    def replies(self, action: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("action") == action]


@dataclass
class Estate_:
    app_id: str
    estate: Estate
    users: Dict[str, User] = field(default_factory=dict)
    sockets: List[ChatConnection] = field(default_factory=list)

    def token(self, key: str) -> str:
        user = self.users[key]
        return create_access_token({"sub": user.email, "roles": [r.value for r in user.roles], "app_id": self.app_id})

    def headers(self, key: str) -> Dict[str, str]:
        return {"X-App-Id": self.app_id, "Authorization": f"Bearer {self.token(key)}"}

    async def socket(self, key: str) -> FakeSocket:
        sock = FakeSocket()
        conn = ChatConnection(websocket=sock, user_id=str(self.users[key].id), app_id=self.app_id)
        await chat_hub.connect(conn)
        self.sockets.append(conn)
        sock.conn = conn
        return sock


@pytest.fixture
async def estate(db_session):
    estate_id = uuid.uuid4()
    app_id = f"RP-{estate_id.hex[:6].upper()}"
    estate = Estate(id=estate_id, app_id=app_id, name="Chat Estate")
    db_session.add(estate)
    db_session.add(Subscription(
        estate_id=estate_id, status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).date(),
    ))
    ctx = Estate_(app_id=app_id, estate=estate)
    tag = estate_id.hex[:8]

    def add(key, name, roles, house=None, street=None, fcm=True):
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email=f"{key}-{tag}@chat.test", full_name=name, hashed_password="hashed",
            roles=roles, app_id=app_id, house_number=house, street_name=street,
            fcm_token=f"fcm-{key}-{tag}" if fcm else None,
        )
        db_session.add(user)
        ctx.users[key] = user

    add("caretaker", "Estate Caretaker", [UserRole.CARETAKER], fcm=False)
    add("resident", "Ada Obi", [UserRole.RESIDENT], house="12", street="Palm Avenue")
    add("resident2", "Bola Ade", [UserRole.RESIDENT], house="3", street="Oak Close")
    add("resident3", "Chidi Eze", [UserRole.LANDLORD], house="7", street="Elm Road")
    add("guard1", "Officer James", [UserRole.SECURITY])
    add("guard2", "Officer Grace", [UserRole.SECURITY])
    add("guard3", "Officer North", [UserRole.SECURITY])
    await db_session.commit()

    r_client = get_redis_client()
    if r_client:
        try:
            await r_client.delete(CHAT_MESSAGE_QUEUE_KEY)
        except Exception:
            pass

    yield ctx

    for conn in ctx.sockets:
        await chat_hub.disconnect(conn)
    for user in ctx.users.values():
        await presence_store.clear_active_room(user.id)
        await presence_store.remove_guard_duty(app_id, user.id)
    if r_client:
        try:
            await r_client.delete(CHAT_MESSAGE_QUEUE_KEY)
        except Exception:
            pass


@pytest.fixture
def push_mock():
    async def fake_send(token, title, body, data=None, high_priority=True):
        return PushResult(token=token, status=PushStatus.SENT)

    with patch("app.services.chat.fcm_service.send_to_token", new_callable=AsyncMock, side_effect=fake_send) as mock:
        yield mock


def pushed_tokens(mock) -> List[str]:
    return [c.args[0] for c in mock.await_args_list]


async def create_gate(client, ctx: Estate_, name: str, code: str) -> Dict[str, Any]:
    resp = await client.post("/api/v1/chat/gates", json={"name": name, "code": code}, headers=ctx.headers("caretaker"))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def go_on_duty(client, ctx: Estate_, guard: str, gate_id: str, badge: str, status: str = "on_duty"):
    resp = await client.post(
        "/api/v1/chat/guard/duty",
        json={"status": status, "gate_site_id": gate_id, "badge": badge},
        headers=ctx.headers(guard),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def open_thread(client, ctx: Estate_, resident: str = "resident", **body) -> Dict[str, Any]:
    body.setdefault("initial_message", "Hello gatehouse, I'm expecting a delivery.")
    resp = await client.post("/api/v1/chat/threads", json=body, headers=ctx.headers(resident))
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- §1 Access rules ---

async def test_guard_cannot_initiate_thread(client, estate, push_mock):
    await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    resp = await client.post(
        "/api/v1/chat/threads",
        json={"initial_message": "Guard trying to start a chat"},
        headers=estate.headers("guard1"),
    )
    assert resp.status_code == 403
    assert push_mock.await_count == 0


async def test_only_caretaker_can_create_gates(client, estate):
    resp = await client.post("/api/v1/chat/gates", json={"name": "Rogue Gate"}, headers=estate.headers("resident"))
    assert resp.status_code == 403


async def test_resident_targets_gate_without_selecting_guard(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    north = await create_gate(client, estate, "North Gate", "GATE-N")

    gates = await client.get("/api/v1/chat/gates", headers=estate.headers("resident"))
    assert gates.status_code == 200
    assert [g["name"] for g in gates.json()] == ["Main Gate Alpha", "North Gate"]

    targeted = await open_thread(client, estate, gate_site_id=north["id"])
    assert targeted["gate_site_id"] == north["id"]
    assert targeted["gate_site_name"] == "North Gate"
    assert targeted["unit_address"] == "12, Palm Avenue"
    assert targeted["guard_unread_count"] == 1
    assert targeted["messages"][0]["sender_role"] == "resident"

    defaulted = await open_thread(client, estate)
    assert defaulted["gate_site_id"] == main["id"], "omitting the gate routes to the primary gate"

    missing = await client.post(
        "/api/v1/chat/threads",
        json={"gate_site_id": str(uuid.uuid4()), "initial_message": "Hi"},
        headers=estate.headers("resident"),
    )
    assert missing.status_code == 404


async def test_other_resident_cannot_read_thread(client, estate, push_mock):
    await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    thread = await open_thread(client, estate)
    resp = await client.get(f"/api/v1/chat/threads/{thread['id']}", headers=estate.headers("resident2"))
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages", json={"message": "sneaky"}, headers=estate.headers("resident2")
    )
    assert resp.status_code == 404


# --- §1 Broadcast & officer tagging ---

async def test_resident_message_reaches_all_guards_at_gate(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    north = await create_gate(client, estate, "North Gate", "GATE-N")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    await go_on_duty(client, estate, "guard2", main["id"], "GH-07", status="on_break")
    await go_on_duty(client, estate, "guard3", north["id"], "GH-11")

    g1, g2, g3 = [await estate.socket(k) for k in ("guard1", "guard2", "guard3")]
    thread = await open_thread(client, estate, gate_site_id=main["id"], initial_message="Visitor at the gate for me")

    for sock in (g1, g2):
        events = sock.of("thread_created")
        assert len(events) == 1
        assert events[0]["thread_id"] == thread["id"]
        assert events[0]["message"]["message"] == "Visitor at the gate for me"
        assert events[0]["in_room"] is False
    assert g3.of("thread_created") == [], "guards posted at other gates are not paged"

    tokens = pushed_tokens(push_mock)
    assert sorted(tokens) == sorted([estate.users["guard1"].fcm_token, estate.users["guard2"].fcm_token])
    data = push_mock.await_args_list[0].args[3]
    assert data["thread_id"] == thread["id"]
    assert data["deep_link"] == f"residence://chat/threads/{thread['id']}"
    assert push_mock.await_args_list[0].args[4] is True


async def test_unstaffed_gate_falls_back_to_all_on_duty_guards(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    north = await create_gate(client, estate, "North Gate", "GATE-N")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    await go_on_duty(client, estate, "guard2", main["id"], "GH-07")
    await go_on_duty(client, estate, "guard3", main["id"], "GH-11", status="off_duty")

    await open_thread(client, estate, gate_site_id=north["id"])
    assert sorted(pushed_tokens(push_mock)) == sorted(
        [estate.users["guard1"].fcm_token, estate.users["guard2"].fcm_token]
    )

    active = await client.get("/api/v1/chat/guard/active", headers=estate.headers("guard1"))
    assert active.status_code == 200
    assert {g["badge"] for g in active.json()} == {"GH-04", "GH-07"}


async def test_no_guard_on_duty_notifies_all_security(client, estate, push_mock):
    await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await open_thread(client, estate)
    assert sorted(pushed_tokens(push_mock)) == sorted(
        [estate.users[k].fcm_token for k in ("guard1", "guard2", "guard3")]
    )


async def test_guard_reply_is_tagged_with_officer_details(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    await go_on_duty(client, estate, "guard2", main["id"], "GH-07")
    thread = await open_thread(client, estate)

    g2 = await estate.socket("guard2")
    resident_sock = await estate.socket("resident")
    push_mock.reset_mock()

    resp = await client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages",
        json={"message": "Your delivery has been cleared.", "status_tag": "cleared"},
        headers=estate.headers("guard1"),
    )
    assert resp.status_code == 201, resp.text
    msg = resp.json()
    assert msg["sender_id"] == str(estate.users["guard1"].id)
    assert msg["sender_name"] == "Officer James"
    assert msg["officer_badge"] == "GH-04"
    assert msg["officer_post"] == "Main Gate Alpha"
    assert msg["is_from_guard"] is True
    assert msg["status_tag"] == "CLEARED"

    other_guard_view = g2.of("new_message")
    assert other_guard_view and other_guard_view[0]["message"]["officer_badge"] == "GH-04", \
        "other guards see who answered"
    assert resident_sock.of("new_message")[0]["message"]["sender_name"] == "Officer James"

    assert pushed_tokens(push_mock) == [estate.users["resident"].fcm_token]
    assert push_mock.await_args_list[0].args[1] == "Officer James • Main Gate Alpha"

    detail = await client.get(f"/api/v1/chat/threads/{thread['id']}", headers=estate.headers("resident"))
    assert detail.json()["resident_unread_count"] == 0
    assert [m["sender_role"] for m in detail.json()["messages"]] == ["resident", "security"]


# --- §3 Presence-aware routing ---

async def test_push_suppressed_when_resident_is_in_room(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    thread = await open_thread(client, estate)
    resident_sock = await estate.socket("resident")
    await chat_hub.join_room(resident_sock.conn, thread["id"])
    push_mock.reset_mock()

    url = f"/api/v1/chat/threads/{thread['id']}/messages"
    resp = await client.post(url, json={"message": "Coming to your door now."}, headers=estate.headers("guard1"))
    assert resp.status_code == 201
    assert push_mock.await_count == 0, "active viewers get WebSocket only, no FCM"
    live = resident_sock.of("new_message")[-1]
    assert live["in_room"] is True and live["play_chime"] is True

    await chat_hub.leave_room(resident_sock.conn, thread["id"])
    assert await presence_store.get_active_room(estate.users["resident"].id) is None

    resp = await client.post(url, json={"message": "Are you home?"}, headers=estate.headers("guard1"))
    assert resp.status_code == 201
    assert pushed_tokens(push_mock) == [estate.users["resident"].fcm_token]
    data = push_mock.await_args_list[0].args[3]
    assert data["deep_link"] == f"residence://chat/threads/{thread['id']}"
    assert data["sender_role"] == "security"


async def test_push_only_skips_guards_viewing_this_thread(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    await go_on_duty(client, estate, "guard2", main["id"], "GH-07")
    first = await open_thread(client, estate)
    other = await open_thread(client, estate, resident="resident2")

    await presence_store.set_active_room(estate.users["guard1"].id, first["id"])
    await presence_store.set_active_room(estate.users["guard2"].id, other["id"])
    push_mock.reset_mock()

    resp = await client.post(
        f"/api/v1/chat/threads/{first['id']}/messages", json={"message": "Any update?"},
        headers=estate.headers("resident"),
    )
    assert resp.status_code == 201
    assert pushed_tokens(push_mock) == [estate.users["guard2"].fcm_token], \
        "guard viewing a different thread still gets the push"


# --- Thread list, read state, resolution ---

async def test_guard_thread_list_prioritises_emergencies_then_unread(client, estate, push_mock):
    await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    read_thread = await open_thread(client, estate, resident="resident3", initial_message="Old question")
    await client.get(f"/api/v1/chat/threads/{read_thread['id']}", headers=estate.headers("guard1"))
    unread = await open_thread(client, estate, resident="resident2", initial_message="Newer question")
    emergency = await open_thread(client, estate, is_emergency=True, initial_message="Intruder near block C!")
    newest_read = await open_thread(client, estate, resident="resident3", initial_message="Latest but read")
    await client.get(f"/api/v1/chat/threads/{newest_read['id']}", headers=estate.headers("guard1"))

    assert emergency["has_active_emergency"] is True
    assert emergency["messages"][0]["status_tag"] == "EMERGENCY"

    resp = await client.get("/api/v1/chat/threads", headers=estate.headers("guard1"))
    assert resp.status_code == 200
    order = [t["id"] for t in resp.json()]
    assert order == [emergency["id"], unread["id"], newest_read["id"], read_thread["id"]]

    mine = await client.get("/api/v1/chat/threads", headers=estate.headers("resident2"))
    assert [t["id"] for t in mine.json()] == [unread["id"]]


async def test_opening_thread_marks_messages_read(client, estate, db_session, push_mock):
    await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    thread = await open_thread(client, estate)
    await client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages", json={"message": "Second note"},
        headers=estate.headers("resident"),
    )

    resp = await client.get(f"/api/v1/chat/threads/{thread['id']}", headers=estate.headers("guard2"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["guard_unread_count"] == 0
    assert len(body["messages"]) == 2
    assert all(m["is_read"] and m["read_at"] for m in body["messages"])

    rows = (await db_session.execute(
        select(ChatMessage)
        .where(ChatMessage.thread_id == uuid.UUID(thread["id"]))
        .execution_options(populate_existing=True)
    )).scalars().all()
    assert all(m.is_read and m.read_at for m in rows)


async def test_resolve_and_reopen(client, estate, db_session, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    thread = await open_thread(client, estate, is_emergency=True)

    resp = await client.post(f"/api/v1/chat/threads/{thread['id']}/resolve", headers=estate.headers("guard1"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["has_active_emergency"] is False
    assert resp.json()["last_message_text"] == "Marked as resolved by Officer James (Main Gate Alpha)"

    blocked = await client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages", json={"message": "One more update from the gate"},
        headers=estate.headers("guard1"),
    )
    assert blocked.status_code == 409
    assert "resident can reopen" in blocked.json()["detail"]

    resp = await client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages", json={"message": "Actually, one more thing"},
        headers=estate.headers("resident"),
    )
    assert resp.status_code == 201
    stored = await db_session.get(ChatThread, uuid.UUID(thread["id"]))
    await db_session.refresh(stored)
    assert stored.status == "open"


async def test_duty_endpoint_requires_security_role(client, estate):
    resp = await client.post("/api/v1/chat/guard/duty", json={"status": "on_duty"}, headers=estate.headers("resident"))
    assert resp.status_code == 403


async def test_off_duty_guard_keeps_tagging_info(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    thread = await open_thread(client, estate)
    off = await go_on_duty(client, estate, "guard1", main["id"], None, status="off_duty")
    assert off["badge"] == "GH-04"

    resp = await client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages", json={"message": "Handled before shift end"},
        headers=estate.headers("guard1"),
    )
    assert resp.json()["officer_badge"] == "GH-04"
    active = await client.get("/api/v1/chat/guard/active", headers=estate.headers("guard2"))
    assert active.json() == []


# --- §5 WebSocket protocol ---

async def test_websocket_rejects_bad_token(client):
    ws = FakeWebSocket([])
    await chat_websocket(ws, token="not-a-jwt")
    assert ws.accepted is False
    assert ws.close_code == WS_CLOSE_UNAUTHORIZED


async def test_websocket_join_send_ping_leave(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    thread = await open_thread(client, estate)
    resident_sock = await estate.socket("resident")
    push_mock.reset_mock()

    ws = FakeWebSocket([
        {"action": "ping"},
        {"action": "join_room", "thread_id": thread["id"]},
        {"action": "send_message", "thread_id": thread["id"], "text": "On my way", "client_id": "c-1"},
        {"action": "join_room", "thread_id": str(uuid.uuid4())},
        {"action": "dance"},
        {"action": "leave_room", "thread_id": thread["id"]},
    ])
    guard_id = str(estate.users["guard1"].id)
    observed_room = {}

    original_leave = chat_hub.leave_room

    async def spy_leave(conn, thread_id=None):
        observed_room["before_leave"] = await presence_store.get_active_room(guard_id)
        await original_leave(conn, thread_id)

    with patch.object(chat_hub, "leave_room", side_effect=spy_leave):
        await chat_websocket(ws, token=estate.token("guard1"))

    assert ws.accepted
    assert ws.replies("connected")[0]["is_guard"] is True
    assert ws.replies("pong")
    assert ws.replies("room_joined")[0]["thread_id"] == thread["id"]

    sent = ws.replies("message_sent")[0]
    assert sent["client_id"] == "c-1"
    assert sent["message"]["officer_badge"] == "GH-04"
    assert sent["message"]["officer_post"] == "Main Gate Alpha"

    errors = ws.replies("error")
    assert [e.get("status_code") for e in errors] == [404, None]
    assert ws.replies("room_left")

    assert observed_room["before_leave"] == thread["id"]
    assert await presence_store.get_active_room(guard_id) is None
    assert chat_hub.local_connection_count(guard_id) == 0

    assert resident_sock.of("new_message")[0]["message"]["message"] == "On my way"
    assert resident_sock.of("thread_read"), "guard joining the room clears the resident's message as read"

    for task in list(chat_hub._background):
        await task
    assert pushed_tokens(push_mock) == [estate.users["resident"].fcm_token]


async def test_websocket_resident_cannot_join_foreign_thread(client, estate, push_mock):
    await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    thread = await open_thread(client, estate)
    ws = FakeWebSocket([{"action": "join_room", "thread_id": thread["id"]}])
    await chat_websocket(ws, token=estate.token("resident2"))
    assert ws.replies("error")[0]["status_code"] == 404
    assert await presence_store.get_active_room(estate.users["resident2"].id) is None


async def test_websocket_guard_cannot_send_after_resolve(client, estate, push_mock):
    main = await create_gate(client, estate, "Main Gate Alpha", "GATE-A")
    await go_on_duty(client, estate, "guard1", main["id"], "GH-04")
    thread = await open_thread(client, estate)
    assert (await client.post(
        f"/api/v1/chat/threads/{thread['id']}/resolve",
        headers=estate.headers("guard1"),
    )).status_code == 200

    ws = FakeWebSocket([{
        "action": "send_message",
        "thread_id": thread["id"],
        "text": "One more update from the gate",
    }])
    await chat_websocket(ws, token=estate.token("guard1"))

    error = ws.replies("error")[0]
    assert error["status_code"] == 409
    assert "resident can reopen" in error["detail"]
    assert ws.replies("message_sent") == []


@pytest.mark.asyncio
async def test_caretaker_audit_sees_all_estate_threads_without_reading(client, estate, db_session, push_mock):
    first = await open_thread(client, estate)
    second = await open_thread(client, estate, resident="resident2")
    before = await db_session.get(ChatThread, uuid.UUID(first['id']))
    unread = before.guard_unread_count
    listing = await client.get('/api/v1/chat/audit/threads', headers=estate.headers('caretaker'))
    assert listing.status_code == 200
    assert {t['id'] for t in listing.json()['threads']} >= {first['id'], second['id']}
    detail = await client.get(f"/api/v1/chat/audit/threads/{first['id']}", headers=estate.headers('caretaker'))
    assert detail.status_code == 200
    assert detail.json()['messages']
    assert detail.json()['messages'][0]['is_read'] is False
    await db_session.refresh(before)
    assert before.guard_unread_count == unread


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['resident', 'guard1'])
async def test_chat_audit_denies_participants(client, estate, role, push_mock):
    thread = await open_thread(client, estate)
    response = await client.get('/api/v1/chat/audit/threads', headers=estate.headers(role))
    assert response.status_code == 403
    response = await client.get(f"/api/v1/chat/audit/threads/{thread['id']}", headers=estate.headers(role))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_chat_audit_is_tenant_scoped_and_searchable(client, estate, db_session, push_mock):
    own = await open_thread(client, estate, initial_message="Unique parcel inquiry")
    await open_thread(client, estate, resident="resident2", initial_message="Gate access question")
    foreign = ChatThread(app_id="OTHER-ESTATE", resident_id=estate.users["resident"].id,
                         resident_name="Other estate resident", last_message_at=datetime.now(UTC).replace(tzinfo=None))
    db_session.add(foreign)
    await db_session.commit()
    response = await client.get('/api/v1/chat/audit/threads?limit=1', headers=estate.headers('caretaker'))
    assert response.status_code == 200
    assert response.json()['has_more'] is True
    assert len(response.json()['threads']) == 1
    response = await client.get('/api/v1/chat/audit/threads?search=Unique%20parcel', headers=estate.headers('caretaker'))
    assert response.status_code == 200
    assert [t['id'] for t in response.json()['threads']] == [own['id']]
    response = await client.get(f'/api/v1/chat/audit/threads/{foreign.id}', headers=estate.headers('caretaker'))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_removing_participant_preserves_conversation_log(client, estate, db_session, push_mock):
    thread = await open_thread(client, estate)
    from app.services.users import delete_user
    assert await delete_user(db_session, estate.users['resident'].id, estate.app_id)
    await db_session.refresh(estate.users['resident'])
    assert estate.users['resident'].is_revoked is True
    detail = await client.get(f"/api/v1/chat/audit/threads/{thread['id']}", headers=estate.headers('caretaker'))
    assert detail.status_code == 200
    assert detail.json()['messages'][0]['sender_name'] == 'Ada Obi'

async def test_offline_resident_receives_reply_on_every_registered_device(client, estate, db_session, push_mock):
    from app.models.sos import PushDevice
    resident = estate.users['resident']
    db_session.add_all([
        PushDevice(token='resident-second-device', user_id=resident.id),
        PushDevice(token=resident.fcm_token, user_id=resident.id),
        PushDevice(token='other-household-device', user_id=estate.users['resident2'].id),
    ])
    await db_session.commit()
    thread = await open_thread(client, estate)
    push_mock.reset_mock()
    response = await client.post(f"/api/v1/chat/threads/{thread['id']}/messages", json={'message': 'Your visitor has arrived'}, headers=estate.headers('guard1'))
    assert response.status_code == 201, response.text
    assert sorted(pushed_tokens(push_mock)) == sorted([resident.fcm_token, 'resident-second-device'])
    assert all(c.args[3]['thread_id'] == thread['id'] for c in push_mock.await_args_list)
