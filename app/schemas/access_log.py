from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime
import uuid

class AccessLogSchema(BaseModel):
    id: uuid.UUID
    created_at: datetime
    app_id: str
    event_type: str
    identifier: str
    status: str
    denial_reason: Optional[str] = None
    user_id: Optional[uuid.UUID] = None
    resident_id: Optional[uuid.UUID] = None
    visitor_name: Optional[str] = None
    details: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class AccessLogHistoryResponseSchema(BaseModel):
    tier: str
    log_retention_days: int
    earliest_accessible_date: datetime
    total_returned: int
    logs: List[AccessLogSchema]

# Alias for backwards compatibility with admin endpoint
AccessLogResponseSchema = AccessLogSchema
