"""Estate-scoped settings snapshots and silent FCM invalidations."""
import logging
from sqlalchemy import select
from fastapi import HTTPException
from app.models.estate import Estate
from app.models.user import User
from app.models.sos import PushDevice
from app.services.fcm import fcm_service

async def snapshot(db, app_id):
    result = await db.execute(
        select(Estate.name, Estate.token_code_length).where(Estate.app_id == app_id)
    )
    estate = result.one_or_none()
    if estate is None:
        raise HTTPException(404, 'Estate not found')
    return {
        'app_id': app_id,
        'estate_name': estate.name,
        'token_code_length': estate.token_code_length,
    }

async def recipients(db, app_id):
    devices = (await db.execute(select(PushDevice.token).join(User, User.id == PushDevice.user_id).where(User.app_id == app_id, User.is_revoked.is_(False)))).scalars().all()
    legacy = (await db.execute(select(User.fcm_token).where(User.app_id == app_id, User.is_revoked.is_(False), User.fcm_token.is_not(None)))).scalars().all()
    return list(dict.fromkeys(t for t in [*devices, *legacy] if t))

async def notify(tokens, app_id):
    # Invalidation only: clients fetch the authoritative snapshot; stale events cannot roll settings back.
    try:
        await fcm_service.send_to_tokens(tokens, '', '', {'type': 'estate_settings_changed', 'app_id': app_id}, high_priority=True)
    except Exception:
        logging.getLogger(__name__).exception('Settings push failed; clients will refresh on resume')
