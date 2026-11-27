"""Incident reports filed by estate members and caretaker announcements to the estate."""
import logging
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, func
from app.models.base import utc_now
from app.models.community import IncidentReport, Announcement, SalesInquiry
from app.models.estate import Estate
from app.models.sos import PushDevice
from app.models.user import User, UserRole
from app.services import notification_inbox
from app.services.fcm import fcm_service, PushStatus

logger = logging.getLogger(__name__)

REPORTS_PER_HOUR = 10
ANNOUNCEMENTS_PER_HOUR = 20


def is_caretaker(user: User) -> bool:
    return UserRole.CARETAKER in user.roles


def require_caretaker(user: User) -> None:
    if not is_caretaker(user):
        raise HTTPException(403, 'Estate caretaker access required.')


def primary_role(user: User) -> str:
    for role in (UserRole.CARETAKER, UserRole.SECURITY, UserRole.LANDLORD, UserRole.RESIDENT):
        if role in user.roles:
            return role.value
    return UserRole.RESIDENT.value


def iso(value) -> str:
    return value.isoformat() + 'Z'


def report_view(report: IncidentReport, viewer: User, reporter: User | None = None) -> dict:
    own = report.reporter_id == viewer.id
    name = 'You (anonymous)' if report.is_anonymous and own else 'Anonymous' if report.is_anonymous else report.reporter_name or 'Estate member'
    identity = {}
    if is_caretaker(viewer):
        identity = {
            'reporter_id': str(report.reporter_id) if report.reporter_id else None,
            'reporter_identity': report.reporter_name or (
                (reporter.full_name or reporter.email) if reporter else None),
        }
    return {**identity, 'id': str(report.id), 'title': report.title, 'description': report.description,
            'location': report.location, 'severity': report.severity, 'status': report.status,
            'is_anonymous': report.is_anonymous, 'reporter_role': report.reporter_role,
            'reported_by_name': name, 'is_own': own, 'resolution_note': report.resolution_note,
            'created_at': iso(report.created_at), 'updated_at': iso(report.updated_at)}


async def create_report(db, user: User, body) -> dict:
    since = utc_now() - timedelta(hours=1)
    recent = await db.scalar(select(func.count()).select_from(IncidentReport).where(
        IncidentReport.app_id == user.app_id, IncidentReport.reporter_id == user.id, IncidentReport.created_at >= since))
    if recent >= REPORTS_PER_HOUR:
        raise HTTPException(429, 'You have filed several reports recently. Please wait before filing another.')
    report = IncidentReport(app_id=user.app_id, reporter_id=user.id, reporter_role=primary_role(user),
                            reporter_name=None if body.is_anonymous else (user.full_name or user.email),
                            is_anonymous=body.is_anonymous, title=body.title, description=body.description,
                            location=body.location, severity=body.severity, status='open')
    db.add(report)
    await db.flush()
    caretakers = (await db.execute(select(User.id).where(
        User.app_id == user.app_id, User.is_revoked.is_(False), User.roles.contains([UserRole.CARETAKER])))).scalars().all()
    for caretaker_id in caretakers:
        if caretaker_id != user.id:
            await notification_inbox.record(db, caretaker_id, user.app_id, f'incident:{report.id}',
                f'New {report.severity} incident report', report.title,
                {'type': 'incident_report', 'report_id': str(report.id)})
    await db.commit()
    await db.refresh(report)
    return report_view(report, user, user)


async def list_reports(db, user: User, status: str | None, severity: str | None, offset: int, limit: int) -> dict:
    query = select(IncidentReport).where(IncidentReport.app_id == user.app_id)
    if not is_caretaker(user):
        query = query.where(IncidentReport.reporter_id == user.id)
    if status:
        query = query.where(IncidentReport.status == status)
    if severity:
        query = query.where(IncidentReport.severity == severity)
    rows = (await db.execute(query.order_by(IncidentReport.created_at.desc(), IncidentReport.id.desc())
                             .offset(offset).limit(limit + 1))).scalars().all()
    counts = {}
    if is_caretaker(user):
        counts = dict((await db.execute(select(IncidentReport.status, func.count()).where(
            IncidentReport.app_id == user.app_id).group_by(IncidentReport.status))).all())
    reporters = {}
    if is_caretaker(user):
        ids = {r.reporter_id for r in rows[:limit] if r.reporter_id and not r.reporter_name}
        if ids:
            accounts = (await db.execute(select(User).where(
                User.id.in_(ids), User.app_id == user.app_id))).scalars()
            reporters = {account.id: account for account in accounts}
    return {'reports': [report_view(r, user, reporters.get(r.reporter_id)) for r in rows[:limit]], 'has_more': len(rows) > limit,
            'counts': {s: counts.get(s, 0) for s in ('open', 'investigating', 'resolved')}}


async def update_report(db, user: User, report_id, body) -> dict:
    require_caretaker(user)
    report = (await db.execute(select(IncidentReport).where(
        IncidentReport.id == report_id, IncidentReport.app_id == user.app_id).with_for_update())).scalar_one_or_none()
    if not report:
        raise HTTPException(404, 'Report not found in your estate.')
    previous = report.status
    if body.status is not None:
        report.status = body.status
    if body.resolution_note is not None:
        report.resolution_note = body.resolution_note.strip() or None
    report.handled_by = user.id
    if report.reporter_id and report.status != previous:
        await notification_inbox.record(db, report.reporter_id, user.app_id, f'incident:{report.id}:{report.status}',
            'Incident report updated', f'"{report.title}" is now {report.status}.',
            {'type': 'incident_report', 'report_id': str(report.id), 'status': report.status})
    await db.commit()
    await db.refresh(report)
    reporter = None
    if report.reporter_id and not report.reporter_name:
        reporter = await db.scalar(select(User).where(
            User.id == report.reporter_id, User.app_id == user.app_id))
    return report_view(report, user, reporter)


def announcement_view(item: Announcement) -> dict:
    return {'id': str(item.id), 'title': item.title, 'body': item.body, 'category': item.category,
            'audience': item.audience, 'author_name': item.author_name, 'recipient_count': item.recipient_count,
            'push_sent': item.push_sent, 'push_failed': item.push_failed, 'created_at': iso(item.created_at)}


async def create_announcement(db, user: User, body) -> dict:
    require_caretaker(user)
    since = utc_now() - timedelta(hours=1)
    recent = await db.scalar(select(func.count()).select_from(Announcement).where(
        Announcement.app_id == user.app_id, Announcement.created_at >= since))
    if recent >= ANNOUNCEMENTS_PER_HOUR:
        raise HTTPException(429, 'Your estate has sent many announcements this hour. Please wait before sending another.')
    recipients = select(User.id).where(User.app_id == user.app_id, User.is_revoked.is_(False),
                                       ~User.roles.contains([UserRole.SAAS_OWNER]), User.id != user.id)
    if body.audience != 'all':
        recipients = recipients.where(User.roles.contains([UserRole(body.audience)]))
    if body.audience == UserRole.RESIDENT.value:
        recipients = recipients.where(User.landlord_id.is_not(None))
    recipient_ids = (await db.execute(recipients)).scalars().all()
    item = Announcement(app_id=user.app_id, author_id=user.id, author_name=user.full_name or user.email,
                        title=body.title, body=body.body, category=body.category, audience=body.audience,
                        recipient_count=len(recipient_ids))
    db.add(item)
    await db.flush()
    data = {'type': 'announcement', 'announcement_id': str(item.id), 'category': item.category, 'app_id': user.app_id}
    for recipient_id in recipient_ids:
        await notification_inbox.record(db, recipient_id, user.app_id, f'announcement:{item.id}', item.title, item.body, data)
    await db.commit()
    # Inbox delivery is durable above; push is a best-effort nudge on top of it.
    tokens = (await db.execute(select(PushDevice.token).where(PushDevice.user_id.in_(recipient_ids)))).scalars().all() if recipient_ids else []
    results = await fcm_service.send_to_tokens(tokens, item.title, item.body, data, high_priority=item.category in ('alert', 'security'))
    item.push_sent = sum(r.status in (PushStatus.SENT, PushStatus.SIMULATED) for r in results)
    item.push_failed = len(results) - item.push_sent
    if item.push_failed:
        logger.warning('Announcement %s push failed for %s device(s)', item.id, item.push_failed)
    await db.commit()
    await db.refresh(item)
    return announcement_view(item)


async def list_announcements(db, user: User, offset: int, limit: int) -> dict:
    require_caretaker(user)
    rows = (await db.execute(select(Announcement).where(Announcement.app_id == user.app_id)
                             .order_by(Announcement.created_at.desc(), Announcement.id.desc())
                             .offset(offset).limit(limit + 1))).scalars().all()
    return {'announcements': [announcement_view(a) for a in rows[:limit]], 'has_more': len(rows) > limit}


SALES_INQUIRIES_PER_DAY = 3


def inquiry_view(item: SalesInquiry, estate_name: str | None) -> dict:
    return {'id': str(item.id), 'app_id': item.app_id, 'estate_name': estate_name, 'tier': item.tier,
            'contact_name': item.contact_name, 'contact_email': item.contact_email, 'phone': item.phone,
            'homes': item.homes, 'message': item.message, 'status': item.status, 'owner_note': item.owner_note,
            'created_at': iso(item.created_at), 'updated_at': iso(item.updated_at)}


async def create_inquiry(db, user: User, body) -> dict:
    require_caretaker(user)
    since = utc_now() - timedelta(days=1)
    recent = await db.scalar(select(func.count()).select_from(SalesInquiry).where(
        SalesInquiry.app_id == user.app_id, SalesInquiry.created_at >= since))
    if recent >= SALES_INQUIRIES_PER_DAY:
        raise HTTPException(429, 'Our sales team already has your request. They will be in touch shortly.')
    item = SalesInquiry(app_id=user.app_id, user_id=user.id, tier='enterprise', status='new',
                        contact_name=body.contact_name, contact_email=str(body.contact_email),
                        phone=body.phone or None, homes=body.homes, message=body.message)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return inquiry_view(item, await db.scalar(select(Estate.name).where(Estate.app_id == user.app_id)))


async def list_inquiries(db, status: str | None, offset: int, limit: int) -> dict:
    query = select(SalesInquiry, Estate.name).outerjoin(Estate, Estate.app_id == SalesInquiry.app_id)
    if status:
        query = query.where(SalesInquiry.status == status)
    rows = (await db.execute(query.order_by(SalesInquiry.created_at.desc(), SalesInquiry.id.desc())
                             .offset(offset).limit(limit + 1))).all()
    counts = dict((await db.execute(select(SalesInquiry.status, func.count()).group_by(SalesInquiry.status))).all())
    return {'inquiries': [inquiry_view(i, name) for i, name in rows[:limit]], 'has_more': len(rows) > limit,
            'counts': {s: counts.get(s, 0) for s in ('new', 'contacted', 'closed')}}


async def update_inquiry(db, inquiry_id, body) -> dict:
    item = await db.get(SalesInquiry, inquiry_id)
    if not item:
        raise HTTPException(404, 'Inquiry not found.')
    if body.status is not None:
        item.status = body.status
    if body.owner_note is not None:
        item.owner_note = body.owner_note.strip() or None
    await db.commit()
    await db.refresh(item)
    return inquiry_view(item, await db.scalar(select(Estate.name).where(Estate.app_id == item.app_id)))
