from unittest.mock import patch, AsyncMock
import pytest
from app.services.fcm import FCMService, PushStatus
from app.core.config import settings


def test_chat_payload_displays_without_running_app_and_survives_offline():
    message = FCMService()._build_message('device', 'Gatehouse', 'Visitor arrived', {'type': 'chat_message', 'thread_id': 'room-1'}, True)['message']
    assert message['notification'] == {'title': 'Gatehouse', 'body': 'Visitor arrived'}
    assert message['android']['priority'] == 'HIGH'
    assert message['android']['ttl'] == '2419200s'
    assert message['data']['thread_id'] == 'room-1'
    assert message['apns']['payload']['aps']['sound'] == 'default'


@pytest.mark.asyncio
async def test_unconfigured_deployment_reports_failure(monkeypatch):
    monkeypatch.setattr(settings, 'ENVIRONMENT', 'production')
    with patch.object(FCMService, 'is_configured', new_callable=lambda: property(lambda self: False)):
        result = await FCMService().send_to_token('device', 'Title', 'Message')
    assert result.status == PushStatus.FAILED
    assert not result.ok
