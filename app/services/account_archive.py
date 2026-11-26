"""Archive expiry and resident requests to restore estate access."""
from datetime import timedelta
from html import escape

from sqlalchemy import String, cast, delete, func, or_, select, update

from app.core.redis import (
    invalidate_cached_registration_token, invalidate_cached_rfid_user,
    invalidate_cached_user, invalidate_visitor_history,
)
from app.models.access_log import AccessLog
from app.models.admin_audit import AdminAuditLog
from app.models.base import utc_now
from app.models.chat import ChatMessage, ChatThread
from app.models.community import Announcement, IncidentReport, SalesInquiry
from app.models.history import HistoryJob
from app.models.onboarding import LandlordImportFile, LandlordOnboardingJob
from app.models.sos import AppNotification, SOSAlert
from app.models.token import RegistrationToken, VisitorToken
from app.models.user import User, UserRole
from app.services import notification_queue
from app.services.notification_inbox import record

ARCHIVE_RETENTION = timedelta(days=30)


async def purge_one(db):
    user = await db.scalar(
        select(User).where(
            User.archived_at <= utc_now() - ARCHIVE_RETENTION,
            User.archive_hold_at.is_(None), User.is_revoked.is_(True),
        ).order_by(User.archived_at, User.id).limit(1)
        .with_for_update(skip_locked=True)
    )
    if user is None:
        return False

    uid, app_id, email, rfid = user.id, user.app_id, user.email, user.rfid_tag
    threads = select(cast(ChatThread.id, String)).where(ChatThread.resident_id == uid)
    messages = select(cast(ChatMessage.id, String)).where(ChatMessage.sender_id == uid)
    sos = select(cast(SOSAlert.id, String)).where(SOSAlert.sender_id == uid)
    await db.execute(delete(AppNotification).where(
        AppNotification.app_id == app_id,
        or_(AppNotification.user_id == uid,
            AppNotification.data["thread_id"].as_string().in_(threads),
            AppNotification.data["message_id"].as_string().in_(messages),
            AppNotification.data["alert_id"].as_string().in_(sos),
            AppNotification.data["archived_user_id"].as_string() == str(uid)),
    ))
    invitation_codes = list((await db.scalars(select(RegistrationToken.code).where(
        RegistrationToken.app_id == app_id, RegistrationToken.landlord_id == uid,
    ))).all())
    # Keep other estate accounts. Their archived host is no longer their parent.
    await db.execute(update(User).where(User.app_id == app_id, User.landlord_id == uid)
                     .values(landlord_id=None, invitation_id=None))
    for model, authored_by in (
        (VisitorToken, VisitorToken.resident_id),
        (RegistrationToken, RegistrationToken.landlord_id),
        (SOSAlert, SOSAlert.sender_id),
        (IncidentReport, IncidentReport.reporter_id),
        (Announcement, Announcement.author_id),
        (SalesInquiry, SalesInquiry.user_id),
        (HistoryJob, HistoryJob.actor_id),
        (LandlordOnboardingJob, LandlordOnboardingJob.user_id),
        (LandlordImportFile, LandlordImportFile.uploaded_by),
    ):
        await db.execute(delete(model).where(model.app_id == app_id, authored_by == uid))
    await db.execute(delete(AccessLog).where(
        AccessLog.app_id == app_id, or_(AccessLog.user_id == uid, AccessLog.resident_id == uid),
    ))
    await db.execute(delete(AdminAuditLog).where(or_(
        AdminAuditLog.actor_id == uid,
        (AdminAuditLog.target_type == "user") & (AdminAuditLog.target_id == str(uid)),
    )))
    # Resident-owned threads, authored chat messages, devices and reset jobs
    # are removed by their existing database cascades.
    await db.delete(user)
    await db.commit()

    await invalidate_cached_user(app_id, email)
    await invalidate_cached_rfid_user(app_id, rfid)
    await invalidate_visitor_history(app_id, uid)
    for code in invitation_codes:
        await invalidate_cached_registration_token(app_id, code)
    return True


def reinstatement_eligible(user, now=None) -> bool:
    """Suspended accounts may ask to be reinstated: switched off by a caretaker, or
    archived from the directory within the retention window. Active accounts never."""
    now = now or utc_now()
    return bool(user.is_revoked) and (user.archived_at is None or user.archived_at > now - ARCHIVE_RETENTION)


async def request_reinstatement(db, app_id: str, email: str) -> None:
    now = utc_now()
    user = await db.scalar(select(User).where(
        User.app_id == app_id,
        or_(User.archived_at.is_(None), User.archived_at > now - ARCHIVE_RETENTION),
        User.is_revoked.is_(True),
        func.lower(func.coalesce(User.archive_email, User.email)) == email.lower(),
    ).order_by(User.archived_at.desc(), User.id).limit(1).with_for_update())
    if user is None:
        return

    key = f"reinstatement:{user.id}:{now.date()}"
    existing = await db.scalar(select(AppNotification.id).where(
        AppNotification.app_id == app_id, AppNotification.event_key == key,
    ).limit(1))
    if existing is not None:
        return

    caretakers = (await db.execute(select(User.id, User.email).where(
        User.app_id == app_id, User.roles.contains([UserRole.CARETAKER]),
        User.is_revoked.is_(False), User.archived_at.is_(None),
    ))).all()
    title = "Account reinstatement request"
    original_email = user.archive_email or user.email
    archived = user.archived_at is not None
    where = "your estate Archive" if archived else (
        "Security staff" if UserRole.SECURITY in user.roles else "your Resident directory")
    body = f"{original_email} requested reinstatement of their suspended account. Review it in {where}."
    for caretaker in caretakers:
        await record(db, caretaker.id, app_id, key, title, body,
                     {"type": "archive_reinstatement" if archived else "account_reinstatement",
                      ("archived_user_id" if archived else "user_id"): str(user.id)})
    await db.commit()
    for caretaker in caretakers:
        await notification_queue.dispatch("email_alert", email=caretaker.email,
                                          title=title, message=escape(body))
