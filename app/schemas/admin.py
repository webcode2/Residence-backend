from pydantic import BaseModel, ConfigDict, EmailStr
from typing import Optional, List, Dict
import uuid
from datetime import datetime, date
from app.core.tiers import SubscriptionTier
from app.schemas.estate import SubscriptionBaseSchema

class SaaSOverviewSchema(BaseModel):
    total_estates: int
    active_estates: int
    suspended_estates: int
    total_users: int
    users_by_role: Dict[str, int]
    subscriptions_by_tier: Dict[str, int]
    estimated_mrr: float
    total_verifications_this_month: int
    total_active_visitors: int

class AdminEstateDetailSchema(BaseModel):
    id: uuid.UUID
    name: str
    app_id: str
    is_active: bool
    token_code_length: int
    created_at: datetime
    subscription: Optional[SubscriptionBaseSchema] = None
    user_count: int
    active_visitors_count: int
    total_verifications: int

    model_config = ConfigDict(from_attributes=True)

class AdminEstateStatusUpdateSchema(BaseModel):
    is_active: bool

class AdminSubscriptionOverrideSchema(BaseModel):
    tier: Optional[SubscriptionTier] = None
    status: Optional[str] = None  # active, expired, cancelled
    expiry_date: Optional[date] = None
    monthly_verifications_limit: Optional[int] = None
    rfid_enabled: Optional[bool] = None
    realtime_alerts_enabled: Optional[bool] = None
    log_retention_days: Optional[int] = None

class AdminUserDetailSchema(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    roles: List[str]
    app_id: str
    estate_name: Optional[str] = None
    rfid_tag: Optional[str] = None
    house_number: Optional[str] = None
    street_name: Optional[str] = None
    is_revoked: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AdminUserRevokeSchema(BaseModel):
    is_revoked: bool

class AdminChangePasswordSchema(BaseModel):
    current_password: str
    new_password: str

class AdminProfileSchema(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str] = None
    roles: List[str]
    app_id: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

