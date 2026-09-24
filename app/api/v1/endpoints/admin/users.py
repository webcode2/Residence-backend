from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
import uuid
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.schemas.admin import AdminUserDetailSchema, AdminUserRevokeSchema
from app.services import admin as admin_service

router = APIRouter()

@router.get("/", response_model=List[AdminUserDetailSchema], tags=["saas_admin_users"])
async def search_users(
    query: Optional[str] = Query(None, description="Search by email, name, RFID, or app_id"),
    role: Optional[str] = Query(None, description="Filter by role"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """Cross-tenant user search across all estates on the platform."""
    return await admin_service.search_users_admin(db, query_str=query, role_filter=role, limit=limit)

@router.patch("/{user_id}/revoke", tags=["saas_admin_users"])
async def set_user_revocation(
    user_id: uuid.UUID,
    revoke_in: AdminUserRevokeSchema,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_saas_admin)
):
    """
    Globally revoke or reinstate a user account.
    Instantly invalidates their cached profile in Redis.
    """
    return await admin_service.set_user_revocation_admin(db, user_id, revoke_in.is_revoked)
