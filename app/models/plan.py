from datetime import datetime
from typing import Optional
from sqlalchemy import String, Numeric, Integer, Boolean, JSON, DateTime, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class SubscriptionPlan(Base):
    __tablename__ = 'subscription_plans'
    tier: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(1000))
    price_monthly: Mapped[float] = mapped_column(Numeric(10, 2))
    monthly_verifications_limit: Mapped[Optional[int]] = mapped_column(Integer().evaluates_none(), nullable=True)
    rfid_enabled: Mapped[bool] = mapped_column(Boolean)
    log_retention_days: Mapped[int] = mapped_column(Integer)
    realtime_alerts_enabled: Mapped[bool] = mapped_column(Boolean)
    discount_6_months: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    discount_12_months: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint('price_monthly > 0'),
        CheckConstraint('discount_6_months >= 0 AND discount_6_months < 100'),
        CheckConstraint('discount_12_months >= 0 AND discount_12_months < 100'),
    )


class EstateCardTrial(Base):
    __tablename__ = 'estate_card_trials'
    app_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    product_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    plan_snapshot: Mapped[dict] = mapped_column(JSON)
    consent_at: Mapped[datetime] = mapped_column(DateTime)
    consent_user_id: Mapped[str] = mapped_column(String(36))
    trial_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default='pending')
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)


class SubscriptionCheckout(Base):
    __tablename__ = 'subscription_checkouts'
    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    reference: Mapped[str] = mapped_column(String(36), unique=True)
    app_id: Mapped[str] = mapped_column(String(50), index=True)
    tier: Mapped[str] = mapped_column(String(30))
    billing_months: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    plan_snapshot: Mapped[dict] = mapped_column(JSON)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
