from pydantic import BaseModel, ConfigDict
from typing import Optional
import uuid
from datetime import datetime

class RegistrationTokenBaseSchema(BaseModel):
    code: str
    landlord_id: uuid.UUID
    house_number: Optional[str] = None
    street_name: Optional[str] = None
    app_id: str

class RegistrationTokenCreateSchema(BaseModel):
    landlord_id: uuid.UUID
    house_number: str
    street_name: str

class RegistrationTokenSchema(RegistrationTokenBaseSchema):
    id: uuid.UUID
    is_used: bool
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class VisitorTokenBaseSchema(BaseModel):
    code: str
    visitor_name: str
    resident_id: uuid.UUID
    app_id: str
    expires_at: datetime

class VisitorTokenCreateSchema(BaseModel):
    visitor_name: str

class VisitorTokenSchema(VisitorTokenBaseSchema):
    id: uuid.UUID
    is_used: bool
    created_at: datetime
    status: str = "pending"
    checked_in_at: Optional[datetime] = None
    checked_out_at: Optional[datetime] = None
    bookout_code: Optional[str] = None
    bookout_expires_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)

class BookOutTokenResponseSchema(BaseModel):
    token_id: uuid.UUID
    visitor_name: str
    bookout_code: str
    status: str
    expires_at: datetime
    message: str = "Present this 4-digit code at the exit gate to book out."

    model_config = ConfigDict(from_attributes=True)
