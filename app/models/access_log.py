from sqlalchemy import String, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin
from typing import Optional
import uuid

class AccessLog(Base, ModelMixin, TenantMixin):
    __tablename__ = "access_logs"
    
    event_type: Mapped[str] = mapped_column(String(50), nullable=False) # rfid_verification, visitor_token_verification
    identifier: Mapped[str] = mapped_column(String(100), nullable=False) # rfid_tag or visitor code
    status: Mapped[str] = mapped_column(String(20), nullable=False) # authorized, denied
    denial_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"), nullable=True)
    resident_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"), nullable=True)
    visitor_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_access_logs_app_id_created_at", "app_id", "created_at"),
        Index("ix_access_logs_app_id_status", "app_id", "status"),
        Index("ix_access_logs_app_id_user_id", "app_id", "user_id"),
    )
