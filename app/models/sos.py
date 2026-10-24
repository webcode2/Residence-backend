from datetime import datetime
import uuid
from sqlalchemy import String, ForeignKey, Integer, DateTime, UniqueConstraint, Boolean, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin

class PushDevice(Base):
    __tablename__ = 'push_devices'
    token: Mapped[str] = mapped_column(String(512), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)

class SOSAlert(Base, ModelMixin, TenantMixin):
    __tablename__ = 'sos_alerts'
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    sender_name: Mapped[str] = mapped_column(String(255))
    house_location: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default='active')
    __table_args__ = (Index('ix_sos_alerts_estate_history', 'app_id', 'created_at', 'id'),)

class SOSDelivery(Base, ModelMixin):
    __tablename__ = 'sos_deliveries'
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('sos_alerts.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    token: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint('alert_id', 'token'),)


class SOSRestriction(Base, ModelMixin, TenantMixin):
    __tablename__ = 'sos_restrictions'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(500))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    __table_args__ = (Index('ix_sos_restrictions_estate_user', 'app_id', 'user_id'),)


class AppNotification(Base, ModelMixin, TenantMixin):
    __tablename__ = 'app_notifications'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    event_key: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(String(2000))
    data: Mapped[dict] = mapped_column(JSON)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint('user_id', 'event_key'),
        Index('ix_app_notifications_user_unread', 'app_id', 'user_id', 'is_read', 'created_at'),
    )
