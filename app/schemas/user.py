from pydantic import BaseModel, EmailStr, ConfigDict, Field, field_validator
from typing import Optional, List
import uuid
from datetime import datetime
from app.models.user import UserRole

class UserBaseSchema(BaseModel):
    email: EmailStr
    phone: Optional[str] = None
    full_name: Optional[str] = None
    roles: List[UserRole]
    rfid_tag: Optional[str] = None
    house_number: Optional[str] = None
    street_name: Optional[str] = None
    fcm_token: Optional[str] = None
    app_id: str

class UserCreateSchema(UserBaseSchema):
    password: str

class ResidentRegistrationSchema(UserCreateSchema):
    # The invitation is authoritative; legacy client fields are never trusted.
    app_id: str = ""
    roles: List[UserRole] = [UserRole.RESIDENT]

class UserUpdateSchema(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    roles: Optional[List[UserRole]] = None
    rfid_tag: Optional[str] = None
    house_number: Optional[str] = None
    street_name: Optional[str] = None
    fcm_token: Optional[str] = None
    is_revoked: Optional[bool] = None
    password: Optional[str] = None

class FCMTokenUpdateSchema(BaseModel):
    fcm_token: str

class UserSchema(UserBaseSchema):
    id: uuid.UUID
    is_revoked: bool
    landlord_id: Optional[uuid.UUID] = None
    must_change_password: bool = False
    invitation_id: Optional[uuid.UUID] = None
    relationship_type: Optional[str] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class LoginRequestSchema(BaseModel):
    email: EmailStr
    password: str

class TokenSchema(BaseModel):
    access_token: str
    token_type: str

class TokenDataSchema(BaseModel):
    email: Optional[str] = None
    app_id: Optional[str] = None


class AuthResponseSchema(BaseModel):
    token: TokenSchema
    user: UserSchema
    token_code_length: int = 6  # Estate-configured length (4-6); included at login for offline caching
class ChangePasswordSchema(BaseModel):
    current_password: str
    new_password: str

class SelfProfileUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: Optional[str] = Field(None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=40)
    house_number: Optional[str] = Field(None, min_length=1, max_length=20)
    street_name: Optional[str] = Field(None, max_length=100)
    current_password: Optional[str] = None

    @field_validator("full_name", "phone", "house_number", "street_name", mode="before")
    @classmethod
    def trim_details(cls, value):
        return value.strip() if isinstance(value, str) else value
