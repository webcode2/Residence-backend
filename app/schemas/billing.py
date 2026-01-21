from pydantic import BaseModel, ConfigDict
from typing import Optional
import uuid
from datetime import datetime

class BillingBaseSchema(BaseModel):
    user_id: uuid.UUID
    amount: float
    description: Optional[str] = None
    title: str
    app_id: str

class BillingCreateSchema(BillingBaseSchema):
    pass

class BillingSchema(BillingBaseSchema):
    id: uuid.UUID
    is_paid: bool
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
