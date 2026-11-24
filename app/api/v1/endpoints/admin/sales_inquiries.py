import uuid
from typing import Literal, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import get_current_saas_admin
from app.models.user import User
from app.services import community

router = APIRouter()
InquiryStatus = Literal['new', 'contacted', 'closed']


class InquiryUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Optional[InquiryStatus] = None
    owner_note: Optional[str] = Field(None, max_length=1000)


@router.get('')
async def sales_inquiries(status: Optional[InquiryStatus] = None, offset: int = Query(0, ge=0, le=100000),
                          limit: int = Query(25, ge=1, le=100), db: AsyncSession = Depends(get_db),
                          admin: User = Depends(get_current_saas_admin)):
    return await community.list_inquiries(db, status, offset, limit)


@router.patch('/{inquiry_id}')
async def update_sales_inquiry(inquiry_id: uuid.UUID, body: InquiryUpdate, db: AsyncSession = Depends(get_db),
                               admin: User = Depends(get_current_saas_admin)):
    return await community.update_inquiry(db, inquiry_id, body)
