import uuid
from typing import Optional
from sqlalchemy import String, ForeignKey, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin


class AdminAuditLog(Base, ModelMixin):
    __tablename__ = 'admin_audit_logs'
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False)
    changes: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index('ix_admin_audit_created_at', 'created_at'),)
