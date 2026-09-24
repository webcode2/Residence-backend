from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.schemas.access_log import AccessLogResponseSchema
from app.services import admin as admin_service

router = APIRouter()

@router.get("/", response_model=List[AccessLogResponseSchema], tags=["saas_admin_access_logs"])
async def get_global_access_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None, description="Filter by status: 'authorized' or 'denied'"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    Platform-wide Security Audit Feed:
    Inspect access verification attempts across all estates and barrier lanes in real time.
    """
    return await admin_service.get_global_access_logs(
        db, limit=limit, offset=offset, status_filter=status
    )
