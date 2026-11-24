import enum
import uuid
from sqlalchemy import String, Boolean, ForeignKey, Index, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import ARRAY
from app.models.base import Base, ModelMixin, TenantMixin

class UserRole(str, enum.Enum):
    SAAS_OWNER = "saas_owner"
    CARETAKER = "caretaker"
    LANDLORD = "landlord"
    RESIDENT = "resident"
    SECURITY = "security"

class User(Base, ModelMixin, TenantMixin):
    __tablename__ = "users"
    
    # Unique per estate (see ix_users_app_id_email): one person may belong to several estates.
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(40), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    invitation_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    relationship_type: Mapped[str] = mapped_column(String(20), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    roles: Mapped[list[UserRole]] = mapped_column(
        ARRAY(Enum(UserRole, name="user_role_enum", inherit_schema=True, values_callable=lambda obj: [e.value for e in obj])),
        nullable=False,
        server_default='{resident}'
    )
    rfid_tag: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=True)
    house_number: Mapped[str] = mapped_column(String(20), nullable=True)
    street_name: Mapped[str] = mapped_column(String(100), nullable=True)
    fcm_token: Mapped[str] = mapped_column(String(512), nullable=True, index=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    
    landlord_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=True)
    
    # Relationships
    landlord: Mapped["User"] = relationship("User", remote_side="User.id", backref="residents")

    __table_args__ = (
        Index("ix_users_app_id_rfid_tag", "app_id", "rfid_tag"),
        Index("ix_users_app_id_email", "app_id", "email", unique=True),
    )


