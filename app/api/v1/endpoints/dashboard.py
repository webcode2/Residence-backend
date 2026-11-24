"""Estate caretaker overview. All data is scoped to the authenticated estate."""
from datetime import timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import check_roles
from app.models.base import utc_now
from app.models.user import User, UserRole
from app.models.chat import ChatThread, GateSite
from app.models.access_log import AccessLog
from app.models.token import VisitorToken
from app.services.chat import list_audit_threads, thread_to_schema

router = APIRouter()


@router.get('/overview')
async def estate_overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(check_roles([UserRole.CARETAKER])),
):
    threads, _ = await list_audit_threads(db, user, limit=5)
    aid = user.app_id
    def count(model, *conditions):
        return select(func.count()).select_from(model).where(model.app_id == aid, *conditions).scalar_subquery()
    summary = (await db.execute(select(
        count(User, User.is_revoked.is_(False), User.roles.contains([UserRole.RESIDENT])).label('residents'),
        count(GateSite, GateSite.is_active.is_(True)).label('active_gates'),
        count(ChatThread, ChatThread.status == 'open').label('open_conversations'),
        count(ChatThread, ChatThread.has_active_emergency.is_(True), ChatThread.status == 'open').label('emergencies'),
    ))).mappings().one()
    today = utc_now().date()
    start = today - timedelta(days=6)
    daily = (await db.execute(select(
        func.date(AccessLog.created_at).label('day'), AccessLog.status, func.count().label('total')
    ).where(AccessLog.app_id == aid, AccessLog.created_at >= start)
      .group_by(func.date(AccessLog.created_at), AccessLog.status))).all()
    counts = {(str(row.day), row.status): row.total for row in daily}
    # Visitor passcodes residents generated, per day, alongside gate outcomes.
    generated = (await db.execute(select(
        func.date(VisitorToken.created_at).label('day'), func.count().label('total')
    ).where(VisitorToken.app_id == aid, VisitorToken.created_at >= start)
      .group_by(func.date(VisitorToken.created_at)))).all()
    counts.update({(str(row.day), 'generated'): row.total for row in generated})
    activity = (await db.execute(select(AccessLog).where(AccessLog.app_id == aid)
        .order_by(AccessLog.created_at.desc(), AccessLog.id.desc()).limit(6))).scalars().all()
    return {
        **dict(summary),
        'gate_activity': [
            {'date': (start + timedelta(days=i)).isoformat(),
             'authorized': counts.get(((start + timedelta(days=i)).isoformat(), 'authorized'), 0),
             'denied': counts.get(((start + timedelta(days=i)).isoformat(), 'denied'), 0),
             'generated': counts.get(((start + timedelta(days=i)).isoformat(), 'generated'), 0)}
            for i in range(7)
        ],
        'recent_activity': [{'id': str(log.id), 'status': log.status,
            'title': log.visitor_name or log.event_type.replace('_', ' '),
            'details': log.details or log.denial_reason or log.event_type.replace('_', ' '),
            'created_at': log.created_at.isoformat()} for log in activity],
        'conversations': [thread_to_schema(thread).model_dump(mode='json') for thread in threads],
        'updated_at': utc_now().isoformat(),
    }
