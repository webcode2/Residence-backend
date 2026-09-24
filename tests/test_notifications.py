import pytest
from unittest.mock import patch, AsyncMock
from app.services.notifications import ResendClient
from app.core.config import settings

@pytest.mark.asyncio
async def test_resend_simulation_when_no_api_key():
    client = ResendClient()
    client.api_key = ""

    # Should simulate and return True without making network calls
    reg_result = await client.send_registration_email("resident@example.com", "temp_pass_123")
    assert reg_result is True

    alert_result = await client.send_realtime_access_alert(
        "resident@example.com", 
        "Gate Entry", 
        "Visitor John Doe has entered the estate."
    )
    assert alert_result is True

@pytest.mark.asyncio
async def test_resend_live_send_registration_email_success():
    client = ResendClient()
    client.api_key = "re_test_key_123456"

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = AsyncMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await client.send_registration_email("alice@sunset.com", "secret123")
        assert result is True

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.resend.com/emails"
        assert kwargs["headers"]["Authorization"] == "Bearer re_test_key_123456"
        assert kwargs["json"]["to"] == ["alice@sunset.com"]
        assert "Welcome to Resident Pass" in kwargs["json"]["html"]

@pytest.mark.asyncio
async def test_resend_live_send_realtime_alert_success():
    client = ResendClient()
    client.api_key = "re_test_key_123456"

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = AsyncMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await client.send_realtime_access_alert(
            "alice@sunset.com", 
            "Visitor Arrival", 
            "Visitor John has checked in at Lane 1."
        )
        assert result is True

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.resend.com/emails"
        assert kwargs["headers"]["Authorization"] == "Bearer re_test_key_123456"
        assert kwargs["json"]["to"] == ["alice@sunset.com"]
        assert kwargs["json"]["subject"] == "Security Alert: Visitor Arrival"

@pytest.mark.asyncio
async def test_resend_api_failure_handled_gracefully():
    client = ResendClient()
    client.api_key = "re_test_key_123456"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = Exception("Network timeout")
        result = await client.send_registration_email("alice@sunset.com", "secret123")
        assert result is False

@pytest.mark.asyncio
async def test_resend_sender_formatting():
    client = ResendClient()
    assert client.sender == f"{settings.RESEND_SENDER_NAME} <{settings.RESEND_SENDER_EMAIL}>"
