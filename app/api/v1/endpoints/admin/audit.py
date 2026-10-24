from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.models.admin_audit import AdminAuditLog

router = APIRouter()

@router.get('')
@router.get('/', include_in_schema=False)
async def admin_audit(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                      db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_saas_admin)):
    rows = (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{'id': str(row.id), 'actor_email': row.actor_email, 'action': row.action, 'target_type': row.target_type,
             'target_id': row.target_id, 'changes': row.changes, 'created_at': row.created_at} for row in rows]
