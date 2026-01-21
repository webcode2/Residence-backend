from pydantic import BaseModel, ConfigDict, EmailStr
from typing import Optional
import uuid
from datetime import date
from app.schemas.user import UserSchema

class EstateBaseSchema(BaseModel):
    name: str

class EstateCreateSchema(EstateBaseSchema):
    pass

class SubscriptionBaseSchema(BaseModel):
    status: str = "active"
    expiry_date: date

class EstateSchema(EstateBaseSchema):
    id: uuid.UUID
    app_id: str
    is_active: bool
    
    model_config = ConfigDict(from_attributes=True)

class EstateWithSubscriptionSchema(EstateSchema):
    subscription: Optional[SubscriptionBaseSchema] = None

class EstateRegistrationRequestSchema(BaseModel):
    estateName: str
    adminName: str
    email: EmailStr
    password: str
    terms: bool

class EstateRegistrationResponseSchema(BaseModel):
    user: UserSchema
    estate_name: str
    app_id: str
