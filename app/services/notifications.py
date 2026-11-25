import httpx
from typing import Optional
from app.core.config import settings
import logging
from html import escape

logger = logging.getLogger(__name__)

class ResendClient:
    """
    Transactional email client powered by Resend (https://resend.com).
    Uses direct asynchronous HTTP requests to Resend's REST API.
    """
    def __init__(self):
        self.api_key = settings.RESEND_API_KEY
        self.base_url = "https://api.resend.com"

    @property
    def sender(self) -> str:
        if settings.RESEND_SENDER_NAME:
            return f"{settings.RESEND_SENDER_NAME} <{settings.RESEND_SENDER_EMAIL}>"
        return settings.RESEND_SENDER_EMAIL

    async def send_registration_email(self, email: str, password: str, full_name: str = "", app_id: str = "", idempotency_key: str | None = None) -> bool:
        """Send account registration email with initial credentials."""
        if not self.api_key:
            logger.error("Welcome email not sent: Resend credentials are missing")
            return False

        recipient = email
        email, password, full_name, app_id = map(escape, (email, password, full_name, app_id))
        email_data = {
            "from": self.sender,
            "to": [recipient],
            "subject": "Your Resident Pass Account",
            "html": f"""
                <h2>Welcome to Resident Pass</h2>
                <p>Hello {full_name}, your Host / Property Owner account has been created.</p>
                <p><b>Estate App ID:</b> {app_id}</p>
                <p><b>Login Email:</b> {email}</p>
                <p><b>Initial Password:</b> {password}</p>
                <p>Sign in to the Residence app with this temporary password. You must choose a new password before accessing the app. You can then invite family members or tenants to your household.</p>
            """,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/emails",
                    json=email_data,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        **({"Idempotency-Key": idempotency_key} if idempotency_key else {})
                    },
                    timeout=10.0
                )
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"Failed to send Resend registration email: {e}")
                return False

    async def send_realtime_access_alert(self, email: str, title: str, message: str) -> bool:
        """Send a real-time gate entry / departure / security notification."""
        if not self.api_key:
            logger.info(f"[REAL-TIME ALERT via Resend] To: {email} | Title: {title} | Message: {message}")
            return True

        email_data = {
            "from": self.sender,
            "to": [email],
            "subject": f"Security Alert: {title}",
            "html": f"""
                <div style="font-family: Arial, sans-serif; padding: 15px; border-left: 4px solid #2563eb; background-color: #f8fafc;">
                    <h2 style="color: #1e293b; margin-top: 0;">{title}</h2>
                    <p style="color: #475569; font-size: 15px;">{message}</p>
                    <p style="color: #94a3b8; font-size: 12px; margin-bottom: 0;">Resident Pass Real-Time Security System</p>
                </div>
            """,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/emails",
                    json=email_data,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    timeout=10.0
                )
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"Failed to send real-time alert email via Resend: {e}")
                return False

resend_client = ResendClient()
notification_service = resend_client

