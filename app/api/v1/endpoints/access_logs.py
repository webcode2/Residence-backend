from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from app.core.database import get_db
from app.schemas.access_log import AccessLogHistoryResponseSchema
from app.services import access_logs as log_service
from app.dependencies import validate_tenant, check_roles
from app.models.user import User, UserRole

router = APIRouter()

@router.get("/", response_model=AccessLogHistoryResponseSchema)
async def get_estate_access_logs(
    limit: int = Query(50, ge=1, le=200, description="Max logs to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    event_type: Optional[str] = Query(None, description="Filter by event type (e.g. rfid_verification, visitor_token_verification)"),
    log_status: Optional[str] = Query(None, alias="status", description="Filter by status (authorized, denied)"),
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER, UserRole.SECURITY]))
):
    """
    Query access verification history for the current estate.
    Results are strictly bounded by the estate's plan tier log retention period
    (Starter: 30 days, Standard: 90 days, Premium: 180 days, Enterprise: 365 days).
    """
    result = await log_service.get_access_logs(
        db=db,
        app_id=app_id,
        limit=limit,
        offset=offset,
        event_type=event_type,
        status=log_status
    )
    return result

@router.post("/cleanup", response_model=dict)
async def cleanup_expired_access_logs(
    app_id: str = Depends(validate_tenant),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_roles([UserRole.CARETAKER]))
):
    """
    Prune logs older than the estate's active tier retention limit.
    Only accessible by Caretakers (Estate Admins).
    """
    pruned_count = await log_service.cleanup_expired_logs(db, app_id)
    return {
        "status": "success",
        "message": f"Pruned {pruned_count} log entries exceeding your tier's retention policy.",
        "pruned_count": pruned_count
    }
