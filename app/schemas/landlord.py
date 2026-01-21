from pydantic import BaseModel, EmailStr
from typing import List, Optional
import uuid

class LandlordImportSchema(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    house_number: Optional[str] = None
    street_name: Optional[str] = None

class LandlordImportResponseSchema(BaseModel):
    email: str
    status: str
    message: Optional[str] = None
