from sqlalchemy import String, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin
from datetime import datetime
import uuid

class RegistrationToken(Base, ModelMixin, TenantMixin):
    __tablename__ = "registration_tokens"
    
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    landlord_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    house_number: Mapped[str] = mapped_column(String(20), nullable=True)
    street_name: Mapped[str] = mapped_column(String(100), nullable=True)
    is_used: Mapped[bool] = mapped_column(default=False)
    is_revoked: Mapped[bool] = mapped_column(default=False)
    
    __table_args__ = (
        Index("ix_reg_tokens_app_id_code", "app_id", "code"),
    )

class VisitorToken(Base, ModelMixin, TenantMixin):
    __tablename__ = "visitor_tokens"
    
    code: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    visitor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    resident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_used: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        Index("ix_vis_tokens_app_id_code", "app_id", "code"),
    )
