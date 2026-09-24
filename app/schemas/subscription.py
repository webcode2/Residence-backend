from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Any
from datetime import date
import uuid
from app.core.tiers import SubscriptionTier

class TierInfoSchema(BaseModel):
    tier: str
    name: str
    price_monthly: float
    monthly_verifications_limit: Optional[int] = None
    rfid_enabled: bool
    log_retention_days: int
    realtime_alerts_enabled: bool
    description: str

class SubscriptionDetailSchema(BaseModel):
    id: uuid.UUID
    estate_id: uuid.UUID
    status: str
    expiry_date: date
    tier: str
    monthly_verifications_limit: Optional[int] = None
    current_month_verifications: int
    verifications_remaining: Optional[int] = None
    billing_cycle_start: date
    rfid_enabled: bool
    log_retention_days: int
    realtime_alerts_enabled: bool
    price_monthly: float

    model_config = ConfigDict(from_attributes=True)

class ChangeTierRequestSchema(BaseModel):
    tier: SubscriptionTier

class CheckoutSessionCreateSchema(BaseModel):
    tier: SubscriptionTier
    success_url: Optional[str] = "https://residence.com/dashboard/billing/success"
    cancel_url: Optional[str] = "https://residence.com/dashboard/billing/cancel"

class CheckoutSessionResponseSchema(BaseModel):
    session_id: str
    checkout_url: str
    tier: str
    amount: float
    currency: str = "USD"
    status: str
    is_simulated: bool = False

class PaymentVerificationResponseSchema(BaseModel):
    session_id: str
    status: str
    tier: str
    is_active: bool
    message: str
    subscription: Optional[SubscriptionDetailSchema] = None

