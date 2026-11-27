import logging

import httpx
import pytest
from unittest.mock import patch, AsyncMock
from app.services.notifications import ResendClient
from app.core.config import settings


def resend_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", "https://api.resend.com/emails"))


@pytest.mark.asyncio
async def test_emails_fail_when_no_api_key():
    client = ResendClient()
    client.api_key = ""

    # No email may pretend delivery without credentials.
    assert await client.send_registration_email("resident@example.com", "temp_pass_123") is False
    assert await client.send_credential_email("reset", "guard@example.com", "temp_pass_123") is False
    assert await client.send_realtime_access_alert(
        "resident@example.com", "Gate Entry", "Visitor John Doe has entered the estate.") is False


@pytest.mark.asyncio
async def test_resend_live_send_registration_email_success():
    client = ResendClient()
    client.api_key = "re_test_key_123456"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = resend_response(200, {"id": "email_1"})
        result = await client.send_registration_email("alice@sunset.com", "secret123")
        assert result is True

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.resend.com/emails"
        assert kwargs["headers"]["Authorization"] == "Bearer re_test_key_123456"
        assert kwargs["json"]["to"] == ["alice@sunset.com"]
        assert "Welcome to Resident Pass" in kwargs["json"]["html"]
        assert kwargs["json"]["subject"] == "Your Resident Pass Account"


@pytest.mark.asyncio
async def test_credential_emails_share_one_template():
    client = ResendClient()
    client.api_key = "re_test_key_123456"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = resend_response(200, {"id": "email_1"})
        for kind, subject in (("staff_welcome", "Your Resident Pass gate officer account"),
                              ("reset", "Your temporary Residence password")):
            assert await client.send_credential_email(kind, "guard@example.com", "p<ss>", "Ada", "EST-1",
                                                      idempotency_key=f"key-{kind}") is True
            kwargs = mock_post.call_args.kwargs
            assert kwargs["json"]["subject"] == subject
            assert kwargs["headers"]["Idempotency-Key"] == f"key-{kind}"
            assert "p&lt;ss&gt;" in kwargs["json"]["html"] and "EST-1" in kwargs["json"]["html"]


@pytest.mark.asyncio
async def test_resend_live_send_realtime_alert_success():
    client = ResendClient()
    client.api_key = "re_test_key_123456"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = resend_response(200, {"id": "email_1"})
        result = await client.send_realtime_access_alert(
            "alice@sunset.com",
            "Visitor Arrival",
            "Visitor <John> has checked in at Lane 1."
        )
        assert result is True

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.resend.com/emails"
        assert kwargs["headers"]["Authorization"] == "Bearer re_test_key_123456"
        assert kwargs["json"]["to"] == ["alice@sunset.com"]
        assert kwargs["json"]["subject"] == "Security Alert: Visitor Arrival"
        assert "Visitor &lt;John&gt;" in kwargs["json"]["html"]


@pytest.mark.asyncio
async def test_resend_rejection_logs_the_reason(caplog):
    client = ResendClient()
    client.api_key = "re_test_key_123456"
    reason = "You can only send testing emails to your own email address"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
            caplog.at_level(logging.ERROR, logger="app.services.notifications"):
        mock_post.return_value = resend_response(403, {"statusCode": 403, "message": reason})
        assert await client.send_credential_email("reset", "guard@example.com", "secret123") is False
    assert "HTTP 403" in caplog.text and reason in caplog.text
    assert "secret123" not in caplog.text


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
