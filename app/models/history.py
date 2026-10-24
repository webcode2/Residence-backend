"""Restart-safe background history exports and confirmed purge jobs."""
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, LargeBinary, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin

class HistoryJob(Base, ModelMixin, TenantMixin):
    __tablename__ = 'history_jobs'
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), index=True)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    actor_email: Mapped[str] = mapped_column(String(255))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime)
    status_filter: Mapped[Optional[str]] = mapped_column(String(20),nullable=True)
    retention_days: Mapped[int] = mapped_column()
    preview_count: Mapped[int] = mapped_column(default=0)
    processed_count: Mapped[int] = mapped_column(default=0)
    cursor_at: Mapped[Optional[datetime]] = mapped_column(DateTime,nullable=True)
    cursor_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime,nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(255),nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime,nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime,nullable=True)
    __table_args__=(Index("ix_history_jobs_purge_range","app_id","end_at",postgresql_where=text("kind = 'purge' AND confirmed_at IS NOT NULL")),)

class HistoryExportChunk(Base):
    __tablename__ = 'history_export_chunks'
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('history_jobs.id',ondelete='CASCADE'),primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
