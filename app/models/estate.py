from sqlalchemy import String, Boolean, Date, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, ModelMixin
from datetime import date
from typing import Optional
import uuid

class Estate(Base, ModelMixin):
    __tablename__ = "estates"
    
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    app_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=True, index=True)
    token_code_length: Mapped[int] = mapped_column(Integer, default=6, server_default="6", nullable=False)
    
    subscription: Mapped["Subscription"] = relationship(back_populates="estate", uselist=False)
    

class Subscription(Base, ModelMixin):
    __tablename__ = "subscriptions"
    
    estate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("estates.id"), unique=True)
    status: Mapped[str] = mapped_column(String(50), default="active")  # active, expired, cancelled
    expiry_date: Mapped[date] = mapped_column(Date)

    # Tier details & feature flags (defaults to Standard with RFID enabled)
    tier: Mapped[str] = mapped_column(String(50), default="standard", nullable=False)
    monthly_verifications_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=50000)
    current_month_verifications: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_cycle_start: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    rfid_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    log_retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    realtime_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    price_monthly: Mapped[float] = mapped_column(Numeric(10, 2), default=30.00, nullable=False)
    
    estate: Mapped["Estate"] = relationship(back_populates="subscription")
