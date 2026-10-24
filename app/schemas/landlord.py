from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import Optional

class LandlordImportSchema(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=5, max_length=40, pattern=r"^\+?[0-9 ()-]+$")
    house_number: str = Field(min_length=1, max_length=20)
    street_name: Optional[str] = Field(default=None, max_length=100)

class LandlordImportResponseSchema(BaseModel):
    email: str
    status: str
    message: Optional[str] = None
    job_id: Optional[str] = None
    batch_id: Optional[str] = None
