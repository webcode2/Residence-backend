"""
Firebase Cloud Messaging push delivery over the FCM HTTP v1 API.

Authenticates with a Google service account (OAuth2 JWT-bearer grant signed locally with
python-jose), caches the access token until shortly before expiry, and sends high-priority
messages with deep-link data. When no service account is configured, sends are logged and
reported as "simulated" only in development; deployed environments fail visibly.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
from jose import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
# backend/app/services/fcm.py -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class PushStatus:
    SENT = "sent"
    SIMULATED = "simulated"
    UNREGISTERED = "unregistered"  # token is stale/invalid; caller should stop using it
    FAILED = "failed"
    NO_TOKEN = "no_token"


@dataclass
class PushResult:
    token: Optional[str]
    status: str
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in (PushStatus.SENT, PushStatus.SIMULATED)


class FCMService:
    def __init__(self) -> None:
        self._credentials: Optional[Dict[str, Any]] = None
        self._credentials_loaded = False
        self._access_token: Optional[str] = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    # --- Configuration ---

    def _load_credentials(self) -> Optional[Dict[str, Any]]:
        if self._credentials_loaded:
            return self._credentials
        self._credentials_loaded = True
        raw = settings.FCM_SERVICE_ACCOUNT_JSON.strip()
        try:
            if raw:
                self._credentials = json.loads(raw)
            elif settings.FCM_SERVICE_ACCOUNT_FILE:
                path = Path(settings.FCM_SERVICE_ACCOUNT_FILE)
                if not path.is_absolute():
                    path = _BACKEND_ROOT / path
                with path.open("r", encoding="utf-8") as fh:
                    self._credentials = json.load(fh)
        except Exception as e:
            logger.error(f"Could not load FCM service account credentials: {e}")
            self._credentials = None
        return self._credentials

    @property
    def project_id(self) -> Optional[str]:
        creds = self._load_credentials() or {}
        return settings.FCM_PROJECT_ID or creds.get("project_id")

    @property
    def is_configured(self) -> bool:
        creds = self._load_credentials()
        return bool(creds and creds.get("private_key") and creds.get("client_email") and self.project_id)

    def reset(self) -> None:
        """Forget cached credentials/tokens (used after settings change and in tests)."""
        self._credentials = None
        self._credentials_loaded = False
        self._access_token = None
        self._access_token_expires_at = 0.0

    # --- OAuth2 ---

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        async with self._token_lock:
            if self._access_token and time.time() < self._access_token_expires_at - 60:
                return self._access_token

            creds = self._load_credentials() or {}
            token_uri = creds.get("token_uri", DEFAULT_TOKEN_URI)
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": creds["client_email"],
                    "scope": FCM_SCOPE,
                    "aud": token_uri,
                    "iat": now,
                    "exp": now + 3600,
                },
                creds["private_key"],
                algorithm="RS256",
                headers={"kid": creds["private_key_id"]} if creds.get("private_key_id") else None,
            )
            response = await client.post(
                token_uri,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                timeout=10.0,
            )
            response.raise_for_status()
            body = response.json()
            self._access_token = body["access_token"]
            self._access_token_expires_at = time.time() + int(body.get("expires_in", 3600))
            return self._access_token

    # --- Sending ---

    def _build_message(
        self,
        token: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]],
        high_priority: bool,
    ) -> Dict[str, Any]:
        # FCM requires every data value to be a string.
        str_data = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in (data or {}).items() if v is not None}
        if (data or {}).get('type') == 'estate_settings_changed':
            return {'message': {'token': token, 'data': str_data,
                'android': {'priority': 'HIGH', 'ttl': '86400s', 'collapse_key': 'estate_settings'},
                'apns': {'headers': {'apns-push-type': 'background', 'apns-priority': '5', 'apns-collapse-id': 'estate_settings'}, 'payload': {'aps': {'content-available': 1}}}}}
        kind = (data or {}).get('type')
        if kind in ('sos_live', 'sos_missed', 'sos_alert'):
            from datetime import datetime, timezone
            from app.services.sos_policy import LIVE_DELIVERY_SECONDS
            try:
                expiry = datetime.fromisoformat(str(data['alarm_expires_at']).replace('Z','+00:00')).timestamp() if data.get('alarm_expires_at') else datetime.fromisoformat(str(data['created_at']).replace('Z','+00:00')).timestamp()+LIVE_DELIVERY_SECONDS
            except (KeyError, TypeError, ValueError):
                expiry = 0
            live = kind in ('sos_live', 'sos_alert') and expiry > time.time()
            if live:
                str_data['type'] = 'sos_live'
            if not live:
                str_data['type'] = 'sos_missed'
                title = 'Estate SOS report'
                if kind != 'sos_missed':
                    body = f"{str_data.get('sender_name', 'An estate member')} reported an SOS at {str_data.get('house_location', 'their home')} ({str_data.get('created_at', 'time unavailable')}). Status: {str_data.get('status', 'unknown')}."
            remaining = max(0, min(LIVE_DELIVERY_SECONDS, int(expiry-time.time()))) if live else 2419200
            return {'message': {'token': token, 'notification': {'title': title, 'body': body}, 'data': str_data,
                'android': {'priority': 'HIGH', 'ttl': f'{remaining}s', 'notification': {
                    'channel_id': 'residence_sos_live_v2' if live else 'residence_sos_reports_v1',
                    'sound': 'sos_alarm' if live else 'default', 'tag': f"sos_{str_data.get('alert_id', '')}",
                    'click_action': 'FLUTTER_NOTIFICATION_CLICK', **({'notification_count': int(data['notification_count'])} if data.get('notification_count') is not None else {})}},
                'apns': {'headers': {'apns-priority': '10', 'apns-expiration': str(int(expiry if live else time.time()+2419200))},
                    'payload': {'aps': {'sound': 'sos_alarm.wav' if live else 'default', 'content-available': 1, **({'badge': int(data['notification_count'])} if data.get('notification_count') is not None else {})}}}}}
        return {"message": {"token": token, "notification": {"title": title, "body": body}, "data": str_data,
            "android": {"priority": "HIGH" if high_priority else "NORMAL", "ttl": "2419200s",
                "notification": {"channel_id": settings.FCM_ANDROID_CHANNEL_ID, "sound": "default", "click_action": "FLUTTER_NOTIFICATION_CLICK", **({"notification_count": int(data["notification_count"])} if data and data.get("notification_count") is not None else {})}},
            "apns": {"headers": {"apns-priority": "10" if high_priority else "5"}, "payload": {"aps": {"sound": "default", "content-available": 1, **({"badge": int(data["notification_count"])} if data and data.get("notification_count") is not None else {})}}}}}

    async def send_to_token(
        self,
        token: Optional[str],
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
        high_priority: bool = True,
    ) -> PushResult:
        if not token:
            return PushResult(token, PushStatus.NO_TOKEN)

        if not self.is_configured:
            if settings.ENVIRONMENT.lower() != "development":
                logger.error("FCM delivery unavailable: service account is not configured")
                return PushResult(token, PushStatus.FAILED, "FCM service account is not configured")
            logger.info(f"[SIMULATED FCM PUSH] token=...{token[-8:]} | {title} | {body} | data={data}")
            return PushResult(token, PushStatus.SIMULATED)

        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"

        async with httpx.AsyncClient() as client:
            for attempt in range(2):
                try:
                    access_token = await self._get_access_token(client)
                    payload = self._build_message(token, title, body, data, high_priority)
                    response = await client.post(
                        url,
                        json=payload,
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=10.0,
                    )
                except Exception as e:
                    logger.error(f"FCM push request failed: {e}")
                    return PushResult(token, PushStatus.FAILED, str(e))

                if response.status_code == 200:
                    return PushResult(token, PushStatus.SENT)
                if response.status_code == 401 and attempt == 0:
                    self._access_token = None
                    continue

                detail = response.text[:500]
                if response.status_code == 404 or "UNREGISTERED" in detail or "registration-token-not-registered" in detail:
                    return PushResult(token, PushStatus.UNREGISTERED, detail)
                if response.status_code == 400 and "INVALID_ARGUMENT" in detail and "token" in detail.lower():
                    return PushResult(token, PushStatus.UNREGISTERED, detail)
                logger.error(f"FCM push rejected ({response.status_code}): {detail}")
                return PushResult(token, PushStatus.FAILED, detail)

        return PushResult(token, PushStatus.FAILED, "authentication retry exhausted")

    async def send_to_tokens(
        self,
        tokens: Iterable[Optional[str]],
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
        high_priority: bool = True,
    ) -> List[PushResult]:
        unique = list(dict.fromkeys(t for t in tokens if t))
        if not unique:
            return []
        return list(await asyncio.gather(
            *(self.send_to_token(t, title, body, data, high_priority) for t in unique)
        ))


fcm_service = FCMService()
