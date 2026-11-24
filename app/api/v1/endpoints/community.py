import uuid
from typing import Literal, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services import community

reports_router = APIRouter()
announcements_router = APIRouter()
sales_router = APIRouter()

Severity = Literal['low', 'medium', 'high']
ReportStatus = Literal['open', 'investigating', 'resolved']


def _trim(value):
    return value.strip() if isinstance(value, str) else value


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    location: str = Field(min_length=2, max_length=200)
    severity: Severity = 'low'
    is_anonymous: bool = False
    _strip = field_validator('title', 'description', 'location', mode='before')(_trim)


class ReportUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Optional[ReportStatus] = None
    resolution_note: Optional[str] = Field(None, max_length=1000)


class AnnouncementCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=3, max_length=2000)
    category: Literal['general', 'alert', 'maintenance', 'security'] = 'general'
    audience: Literal['all', 'resident', 'landlord', 'security'] = 'all'
    _strip = field_validator('title', 'body', mode='before')(_trim)


@reports_router.post('', status_code=201)
async def file_report(body: ReportCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await community.create_report(db, user, body)


@reports_router.get('')
async def reports(status: Optional[ReportStatus] = None, severity: Optional[Severity] = None,
                  offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100),
                  db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await community.list_reports(db, user, status, severity, offset, limit)


@reports_router.patch('/{report_id}')
async def triage_report(report_id: uuid.UUID, body: ReportUpdate, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    return await community.update_report(db, user, report_id, body)


@announcements_router.post('', status_code=201)
async def announce(body: AnnouncementCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await community.create_announcement(db, user, body)


@announcements_router.get('')
async def announcements(offset: int = Query(0, ge=0, le=100000), limit: int = Query(25, ge=1, le=100),
                        db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await community.list_announcements(db, user, offset, limit)


class SalesInquiryCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_name: str = Field(min_length=2, max_length=255)
    contact_email: EmailStr
    phone: Optional[str] = Field(None, max_length=40, pattern=r'^[0-9+()\-\s]*$')
    homes: Optional[int] = Field(None, ge=1, le=100000)
    message: str = Field(min_length=10, max_length=2000)
    _strip = field_validator('contact_name', 'phone', 'message', mode='before')(_trim)


@sales_router.post('', status_code=201)
async def contact_sales(body: SalesInquiryCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await community.create_inquiry(db, user, body)
