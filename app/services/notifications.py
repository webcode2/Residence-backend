import httpx
import base64
from typing import Optional
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class SendPulseClient:
    def __init__(self):
        self.api_id = settings.SENDPULSE_API_ID
        self.api_secret = settings.SENDPULSE_API_SECRET
        self.base_url = "https://api.sendpulse.com"
        self._token: Optional[str] = None

    async def _get_token(self) -> Optional[str]:
        if self._token:
            return self._token
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/oauth/access_token",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.api_id,
                        "client_secret": self.api_secret,
                    },
                )
                response.raise_for_status()
                data = response.json()
                self._token = data.get("access_token")
                return self._token
            except Exception as e:
                logger.error(f"Failed to get SendPulse token: {e}")
                return None

    async def send_registration_email(self, email: str, password: str):
        token = await self._get_token()
        if not token:
            logger.error("Cannot send email: No access token")
            return False

        email_data = {
            "email": {
                "subject": "Your Resident Pass Account",
                "html": f"""
                    <h2>Welcome to Resident Pass</h2>
                    <p>Your account has been created successfully.</p>
                    <p><b>Login Email:</b> {email}</p>
                    <p><b>Initial Password:</b> {password}</p>
                    <p>Please log in and change your password immediately.</p>
                """,
                "from": {
                    "name": settings.SENDPULSE_SENDER_NAME,
                    "email": settings.SENDPULSE_SENDER_EMAIL,
                },
                "to": [
                    {
                        "email": email,
                    }
                ],
            }
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/smtp/emails",
                    json=email_data,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"Failed to send SendPulse email: {e}")
                return False

notification_service = SendPulseClient()
