from sqlalchemy import String, Boolean, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, ModelMixin
from datetime import date
import uuid

class Estate(Base, ModelMixin):
    __tablename__ = "estates"
    
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    app_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=True, index=True)
    
    subscription: Mapped["Subscription"] = relationship(back_populates="estate", uselist=False)
    

class Subscription(Base, ModelMixin):
    __tablename__ = "subscriptions"
    
    estate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("estates.id"), unique=True)
    status: Mapped[str] = mapped_column(String(50), default="active") # active, expired, cancelled
    expiry_date: Mapped[date] = mapped_column(Date)
    
    estate: Mapped["Estate"] = relationship(back_populates="subscription")
