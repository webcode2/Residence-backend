from pydantic import BaseModel, ConfigDict, EmailStr, field_validator
from typing import Optional
import uuid
from datetime import date
from app.schemas.user import UserSchema
from app.core.tiers import SubscriptionTier

class EstateBaseSchema(BaseModel):
    name: str

class EstateCreateSchema(EstateBaseSchema):
    tier: Optional[SubscriptionTier] = SubscriptionTier.STARTER

class SubscriptionBaseSchema(BaseModel):
    status: str = "active"
    expiry_date: date
    tier: str = "starter"
    monthly_verifications_limit: Optional[int] = 20000
    current_month_verifications: int = 0
    rfid_enabled: bool = False
    log_retention_days: int = 30
    realtime_alerts_enabled: bool = False
    price_monthly: float = 15.00

    model_config = ConfigDict(from_attributes=True)

class EstateSchema(EstateBaseSchema):
    id: uuid.UUID
    app_id: str
    is_active: bool
    token_code_length: int = 6
    
    model_config = ConfigDict(from_attributes=True)

class EstateWithSubscriptionSchema(EstateSchema):
    subscription: Optional[SubscriptionBaseSchema] = None

class EstateRegistrationRequestSchema(BaseModel):
    estateName: str
    adminName: str
    email: EmailStr
    password: str
    terms: bool
    tier: Optional[SubscriptionTier] = SubscriptionTier.STARTER

class EstateRegistrationResponseSchema(BaseModel):
    user: UserSchema
    estate_name: str
    app_id: str
    tier: str = "starter"

class EstateSettingsUpdateSchema(BaseModel):
    token_code_length: int

    @field_validator("token_code_length")
    @classmethod
    def validate_code_length(cls, v: int) -> int:
        if v not in [6, 8]:
            raise ValueError("Token code length must be either 6 or 8 characters.")
        return v

class EstateSettingsResponseSchema(BaseModel):
    name: str
    app_id: str
    is_active: bool
    token_code_length: int
    tier: str
    monthly_verifications_limit: Optional[int]
    current_month_verifications: int
    rfid_enabled: bool

    model_config = ConfigDict(from_attributes=True)

