from datetime import datetime, UTC
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
import uuid

def utc_now():
    return datetime.now(UTC).replace(tzinfo=None)

class ModelMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

class TenantMixin:
    app_id: Mapped[str] = mapped_column(String(50), index=True)
