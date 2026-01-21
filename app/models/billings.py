from sqlalchemy import String, Numeric, Boolean, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, ModelMixin, TenantMixin
import uuid

class Billing(Base, ModelMixin, TenantMixin):
    __tablename__ = "billings"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    is_paid: Mapped[bool] = mapped_column(default=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        Index("ix_billings_app_id_user_id", "app_id", "user_id"),
    )
