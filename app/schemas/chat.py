import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ThreadStatus = Literal["open", "resolved", "closed"]
SenderRole = Literal["resident", "security", "system"]
DutyStatus = Literal["on_duty", "on_break", "off_duty"]


# --- Gate Sites ---

class GateSiteCreateSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None, max_length=255)
    is_active: bool = True


class GateSiteSchema(BaseModel):
    id: uuid.UUID
    app_id: str
    estate_id: uuid.UUID
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Messages ---

class ChatMessageCreateSchema(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    status_tag: Optional[str] = Field(None, max_length=50)


class ChatMessageSchema(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    sender_id: uuid.UUID
    sender_role: SenderRole
    sender_name: str
    officer_badge: Optional[str] = None
    officer_post: Optional[str] = None
    is_from_guard: bool
    message: str
    status_tag: Optional[str] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Threads ---

class ChatThreadCreateSchema(BaseModel):
    gate_site_id: Optional[uuid.UUID] = None
    subject: Optional[str] = Field(None, max_length=100)
    initial_message: str = Field(..., min_length=1, max_length=4000)
    is_emergency: bool = False


class ChatThreadSchema(BaseModel):
    id: uuid.UUID
    app_id: str
    resident_id: uuid.UUID
    gate_site_id: Optional[uuid.UUID] = None
    gate_site_name: Optional[str] = None
    unit_address: Optional[str] = None
    resident_name: Optional[str] = None
    subject: Optional[str] = None
    status: ThreadStatus
    has_active_emergency: bool
    last_message_at: datetime
    last_message_text: Optional[str] = None
    last_sender_role: Optional[SenderRole] = None
    resident_unread_count: int
    guard_unread_count: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatThreadDetailSchema(ChatThreadSchema):
    messages: List[ChatMessageSchema] = []


# --- Guard Duty & Presence ---

class GuardDutyUpdateSchema(BaseModel):
    status: DutyStatus
    gate_site_id: Optional[uuid.UUID] = None
    badge: Optional[str] = Field(None, max_length=50)


class GuardPresenceSchema(BaseModel):
    guard_id: uuid.UUID
    name: Optional[str] = None
    status: DutyStatus
    badge: Optional[str] = None
    post_id: Optional[uuid.UUID] = None
    post_name: Optional[str] = None
    updated_at: Optional[datetime] = None


class ChatAuditPageSchema(BaseModel):
    threads: List[ChatThreadSchema]
    has_more: bool
