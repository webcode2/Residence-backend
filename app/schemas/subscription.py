from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Any, Literal
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
    discount_6_months: float = 0
    discount_12_months: float = 0
    quotes: List[dict] = Field(default_factory=list)

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
    billing_months: Literal[1, 6, 12] = 1
    success_url: Optional[str] = "https://residence.com/dashboard/subscription?checkout=success"
    cancel_url: Optional[str] = "https://residence.com/dashboard/subscription?checkout=cancelled"

class CheckoutSessionResponseSchema(BaseModel):
    session_id: str
    checkout_url: str
    tier: str
    amount: float
    currency: str = "USD"
    status: str
    is_simulated: bool = False
    is_sandbox: bool = False
    returns_to_app: bool = True
    billing_months: int = 1
    discount_percent: float = 0
    subtotal: float = 0
    savings: float = 0

class PaymentVerificationResponseSchema(BaseModel):
    session_id: str
    status: str
    tier: str
    is_active: bool
    message: str
    subscription: Optional[SubscriptionDetailSchema] = None

