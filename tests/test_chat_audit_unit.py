"""Audit permission and query checks without external database services."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.core.database import get_db
from app.dependencies import get_current_user
from app.models.user import User, UserRole
from app.services import chat


def caretaker():
    return User(id=uuid.uuid4(), email='caretaker@example.com', roles=[UserRole.CARETAKER],
                app_id='ESTATE-A', hashed_password='unused')


@pytest.mark.asyncio
@pytest.mark.parametrize('role', [UserRole.RESIDENT, UserRole.SECURITY, UserRole.LANDLORD, UserRole.SAAS_OWNER])
async def test_audit_api_rejects_non_caretakers(role):
    user = caretaker()
    user.roles = [role]
    async def user_override():
        return user
    async def db_override():
        yield AsyncMock()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update({get_current_user: user_override, get_db: db_override})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            for path in ('/api/v1/dashboard/overview', '/api/v1/chat/audit/threads', f'/api/v1/chat/audit/threads/{uuid.uuid4()}'):
                response = await client.get(path)
                assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.asyncio
async def test_audit_detail_only_looks_in_authenticated_estate():
    user, tid = caretaker(), uuid.uuid4()
    with patch.object(chat, 'chat_message_queue_length', AsyncMock(return_value=0)), \
         patch.object(chat, '_load_thread', AsyncMock(return_value=None)) as load, \
         patch.object(chat, 'mark_thread_read', AsyncMock()) as mark:
        with pytest.raises(HTTPException) as exc:
            await chat.get_audit_thread(AsyncMock(), user, tid)
        assert exc.value.status_code == 404
        assert load.call_args.args[1:] == ('ESTATE-A', tid)
        mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_list_filters_tenant_search_and_paginates():
    db = AsyncMock()
    db.execute.return_value.scalars = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = ['first', 'second']
    with patch.object(chat, 'chat_message_queue_length', AsyncMock(return_value=0)), \
         patch.object(chat, 'mark_thread_read', AsyncMock()) as mark:
        rows, more = await chat.list_audit_threads(db, caretaker(), search='badge_1%', limit=1, offset=50)
    assert rows == ['first'] and more is True
    statement = db.execute.call_args.args[0]
    values = statement.compile().params
    assert 'ESTATE-A' in values.values()
    assert '%badge\\_1\\%%' in values.values()
    assert statement._limit_clause.value == 2
    assert statement._offset_clause.value == 50
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_history_never_marks_messages_read():
    user, tid, db = caretaker(), uuid.uuid4(), AsyncMock()
    thread = MagicMock(id=tid, guard_unread_count=3, resident_unread_count=2)
    message = MagicMock(is_read=False)
    db.execute.return_value.scalars = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = [message]
    with patch.object(chat, 'chat_message_queue_length', AsyncMock(return_value=0)), \
         patch.object(chat, '_load_thread', AsyncMock(return_value=thread)), \
         patch.object(chat, 'mark_thread_read', AsyncMock()) as mark:
        result, messages = await chat.get_audit_thread(db, user, tid)
    assert result is thread and messages == [message]
    assert message.is_read is False
    assert thread.guard_unread_count == 3 and thread.resident_unread_count == 2
    mark.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_removing_chat_account_revokes_instead_of_deleting_history():
    from app.services import users
    user, db = caretaker(), AsyncMock()
    user.is_revoked, user.fcm_token = False, 'device-token'
    db.scalar.side_effect = [uuid.uuid4(), None, None, None, None, None, None]
    with patch.object(users, 'get_user', AsyncMock(return_value=user)), \
         patch('app.core.redis.invalidate_cached_user', AsyncMock()), \
         patch('app.core.redis.invalidate_cached_rfid_user', AsyncMock()):
        assert await users.delete_user(db, user.id, user.app_id)
    assert user.is_revoked is True and user.fcm_token is None
    db.delete.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_overview_uses_authenticated_estate_for_every_query():
    from app.api.v1.endpoints.dashboard import estate_overview
    db = AsyncMock()
    summary, daily, generated, activity = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    summary.mappings.return_value.one.return_value = {
        'residents': 3, 'active_gates': 2, 'open_conversations': 1, 'emergencies': 0,
    }
    daily.all.return_value = []
    from app.models.base import utc_now
    generated.all.return_value = [MagicMock(day=utc_now().date(), total=4)]
    activity.scalars.return_value.all.return_value = []
    db.execute.side_effect = [summary, daily, generated, activity]
    with patch('app.api.v1.endpoints.dashboard.list_audit_threads', AsyncMock(return_value=([], False))):
        result = await estate_overview(db, caretaker())
    assert result['residents'] == 3
    assert len(result['gate_activity']) == 7
    assert [d['generated'] for d in result['gate_activity']] == [0] * 6 + [4]
    assert result['recent_activity'] == []
    for call in db.execute.call_args_list:
        assert 'ESTATE-A' in call.args[0].compile().params.values()
