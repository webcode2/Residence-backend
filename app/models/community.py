"""Caretaker announcements and resident-filed incident reports."""
import uuid
from sqlalchemy import String, ForeignKey, Integer, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin

REPORT_SEVERITIES = ('low', 'medium', 'high')
REPORT_STATUSES = ('open', 'investigating', 'resolved')
ANNOUNCEMENT_CATEGORIES = ('general', 'alert', 'maintenance', 'security')
ANNOUNCEMENT_AUDIENCES = ('all', 'resident', 'landlord', 'security')


class IncidentReport(Base, ModelMixin, TenantMixin):
    __tablename__ = 'incident_reports'
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    # Snapshot taken at filing time; left empty when the reporter asked to stay anonymous.
    reporter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reporter_role: Mapped[str] = mapped_column(String(20))
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000))
    location: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default='open')
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    handled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    __table_args__ = (
        Index('ix_incident_reports_estate_history', 'app_id', 'created_at', 'id'),
        Index('ix_incident_reports_reporter', 'app_id', 'reporter_id', 'created_at'),
    )


class Announcement(Base, ModelMixin, TenantMixin):
    __tablename__ = 'announcements'
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(2000))
    category: Mapped[str] = mapped_column(String(20))
    audience: Mapped[str] = mapped_column(String(20))
    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    push_sent: Mapped[int] = mapped_column(Integer, default=0)
    push_failed: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (Index('ix_announcements_estate_history', 'app_id', 'created_at', 'id'),)

SALES_INQUIRY_STATUSES = ('new', 'contacted', 'closed')


class SalesInquiry(Base, ModelMixin, TenantMixin):
    """A caretaker's request to talk to sales about the Enterprise offline deployment."""
    __tablename__ = 'sales_inquiries'
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    tier: Mapped[str] = mapped_column(String(20), default='enterprise')
    contact_name: Mapped[str] = mapped_column(String(255))
    contact_email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    homes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default='new')
    owner_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    __table_args__ = (Index('ix_sales_inquiries_status_history', 'status', 'created_at', 'id'),)
