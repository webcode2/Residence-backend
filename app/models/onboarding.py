"""Durable landlord onboarding jobs and private source spreadsheets."""
import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, UniqueConstraint, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin

class LandlordOnboardingJob(Base, ModelMixin, TenantMixin):
    __tablename__ = 'landlord_onboarding_jobs'
    batch_id: Mapped[uuid.UUID] = mapped_column(index=True)
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(40), nullable=True)
    house_number: Mapped[str] = mapped_column(String(20))
    street_name: Mapped[str] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, index=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(255), nullable=True)
    password_ciphertext: Mapped[str] = mapped_column(Text, nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), nullable=True)
    __table_args__ = (UniqueConstraint('app_id', 'email', name='uq_landlord_onboarding_estate_email'),)

class LandlordImportFile(Base, ModelMixin, TenantMixin):
    """Private original spreadsheet; independent of extracted onboarding jobs."""
    __tablename__ = 'landlord_import_files'
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)
    size_bytes: Mapped[int] = mapped_column()
    batch_id: Mapped[uuid.UUID] = mapped_column(index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

class AccountPasswordResetJob(Base, ModelMixin, TenantMixin):
    """Credential email for an existing estate account (officer welcome or password reset).

    The temporary password becomes the account password only after Resend accepts the email.
    """
    __tablename__ = "account_password_reset_jobs"

    kind: Mapped[str] = mapped_column(String(20), default="reset", server_default="reset")  # reset | staff_welcome
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    previous_password_fingerprint: Mapped[str] = mapped_column(String(64))
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
