import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Boolean, ForeignKey, Index, Text, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, ModelMixin, TenantMixin, utc_now

class GateSite(Base, ModelMixin, TenantMixin):
    __tablename__ = "gate_sites"

    estate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("estates.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # e.g., "GATE-ALPHA", "GATE-01"
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    # Relationships
    estate: Mapped["Estate"] = relationship("Estate", backref="gate_sites")
    threads: Mapped[List["ChatThread"]] = relationship("ChatThread", back_populates="gate_site")

    __table_args__ = (
        Index("ix_gate_sites_app_id_is_active", "app_id", "is_active"),
    )


class ChatThread(Base, ModelMixin, TenantMixin):
    __tablename__ = "chat_threads"

    resident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    gate_site_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("gate_sites.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # Denormalized resident information for high-speed guardhouse dispatch
    unit_address: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    resident_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    
    subject: Mapped[Optional[str]] = mapped_column(String(100), nullable=True) # e.g. "Delivery", "Visitor clearance", "Emergency SOS"
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)  # "open", "resolved", "closed"
    has_active_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False, index=True)
    last_message_text: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_sender_role: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    
    resident_unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    guard_unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    resident: Mapped["User"] = relationship("User", foreign_keys=[resident_id])
    gate_site: Mapped[Optional["GateSite"]] = relationship("GateSite", back_populates="threads")
    messages: Mapped[List["ChatMessage"]] = relationship("ChatMessage", back_populates="thread", cascade="all, delete-orphan", order_by="ChatMessage.created_at")

    __table_args__ = (
        Index("ix_chat_threads_app_id_status", "app_id", "status"),
        Index("ix_chat_threads_resident_status", "resident_id", "status"),
        Index("ix_chat_threads_emergency_order", "app_id", "has_active_emergency", "last_message_at"),
    )


class ChatMessage(Base, ModelMixin):
    __tablename__ = "chat_messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    sender_role: Mapped[str] = mapped_column(String(30), nullable=False)  # "resident", "security", "system"
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False)
    
    # Security officer tagging
    officer_badge: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    officer_post: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # e.g., "Main Gate Alpha"
    is_from_guard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    message: Mapped[str] = mapped_column(Text, nullable=False)
    status_tag: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # "CLEARED", "DELIVERY", "DISPATCHED", "EMERGENCY"
    
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    thread: Mapped["ChatThread"] = relationship("ChatThread", back_populates="messages")
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id])
