from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Optional, List
import uuid
from datetime import datetime
from app.models.user import UserRole

class UserBaseSchema(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    roles: List[UserRole]
    rfid_tag: Optional[str] = None
    house_number: Optional[str] = None
    street_name: Optional[str] = None
    app_id: str

class UserCreateSchema(UserBaseSchema):
    password: str

class UserUpdateSchema(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    roles: Optional[List[UserRole]] = None
    rfid_tag: Optional[str] = None
    house_number: Optional[str] = None
    street_name: Optional[str] = None
    is_revoked: Optional[bool] = None
    password: Optional[str] = None

class UserSchema(UserBaseSchema):
    id: uuid.UUID
    is_revoked: bool
    landlord_id: Optional[uuid.UUID] = None
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