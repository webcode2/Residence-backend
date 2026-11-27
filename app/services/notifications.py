"""Every outbound email goes through `ResendClient.send`.

Credential emails (host welcome, security officer welcome, password reset) share one
template, `send_credential_email`. They are sent only by durable Postgres jobs in the
worker (`account_password_resets` for estate accounts, `landlords` for host imports),
which retry until Resend accepts the message.
"""
import logging
from html import escape

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

CREDENTIAL_EMAILS = {
    # kind: (subject, opening line, what the new password unlocks)
    "host_welcome": ("Your Resident Pass Account",
                     "your Host / Property Owner account has been created.",
                     "access the app. You can then invite family members or tenants to your household"),
    "staff_welcome": ("Your Resident Pass gate officer account",
                      "your estate caretaker has registered you as security personnel.",
                      "verify visitors at the gate"),
    "reset": ("Your temporary Residence password",
              "your estate caretaker reset your password.",
              "access your account"),
}


class ResendClient:
    """Transactional email through Resend's REST API (https://resend.com)."""

    def __init__(self):
        self.api_key = settings.RESEND_API_KEY
        self.base_url = "https://api.resend.com"

    @property
    def sender(self) -> str:
        if settings.RESEND_SENDER_NAME:
            return f"{settings.RESEND_SENDER_NAME} <{settings.RESEND_SENDER_EMAIL}>"
        return settings.RESEND_SENDER_EMAIL

    async def send(self, *, to: str, subject: str, html: str, purpose: str,
                   idempotency_key: str | None = None) -> bool:
        """Send one email. True only when Resend accepted it; failures log Resend's reason."""
        if not self.api_key:
            logger.error("%s email not sent: RESEND_API_KEY is not configured", purpose)
            return False
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/emails", headers=headers, timeout=10.0,
                    json={"from": self.sender, "to": [to], "subject": subject, "html": html})
        except Exception as exc:
            logger.error("%s email not sent: %s", purpose, exc)
            return False
        if 200 <= response.status_code < 300:
            return True
        logger.error("%s email rejected by Resend (HTTP %s): %s",
                     purpose, response.status_code, response.text[:300])
        return False

    async def send_credential_email(self, kind: str, email: str, password: str, full_name: str = "",
                                    app_id: str = "", idempotency_key: str | None = None) -> bool:
        """Sign-in details with a temporary password the recipient must change at sign-in."""
        subject, opening, unlocks = CREDENTIAL_EMAILS[kind]
        safe = dict(zip(("email", "password", "name", "app_id"), map(escape, (email, password, full_name, app_id))))
        html = f"""
            <h2>Welcome to Resident Pass</h2>
            <p>Hello {safe["name"]}, {opening}</p>
            <p><b>Estate App ID:</b> {safe["app_id"]}</p>
            <p><b>Login Email:</b> {safe["email"]}</p>
            <p><b>Temporary Password:</b> {safe["password"]}</p>
            <p>Sign in to the Residence app with this temporary password. You must choose a new password
            before you can {unlocks}.</p>
        """
        return await self.send(to=email, subject=subject, html=html, purpose=kind,
                               idempotency_key=idempotency_key)

    async def send_registration_email(self, email: str, password: str, full_name: str = "", app_id: str = "",
                                      idempotency_key: str | None = None) -> bool:
        """Host / Property Owner welcome (sent by the host import job)."""
        return await self.send_credential_email("host_welcome", email, password, full_name, app_id,
                                                idempotency_key=idempotency_key)

    async def send_realtime_access_alert(self, email: str, title: str, message: str) -> bool:
        """Security notice email (for example a reinstatement request to caretakers)."""
        html = f"""
            <div style="font-family: Arial, sans-serif; padding: 15px; border-left: 4px solid #2563eb; background-color: #f8fafc;">
                <h2 style="color: #1e293b; margin-top: 0;">{escape(title)}</h2>
                <p style="color: #475569; font-size: 15px;">{escape(message)}</p>
                <p style="color: #94a3b8; font-size: 12px; margin-bottom: 0;">Resident Pass Real-Time Security System</p>
            </div>
        """
        return await self.send(to=email, subject=f"Security Alert: {title}", html=html, purpose="security_alert")


resend_client = ResendClient()
notification_service = resend_client
