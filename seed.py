import asyncio
from datetime import datetime, timedelta, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import SessionLocal, engine
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import RegistrationToken, VisitorToken
from app.core.tiers import SubscriptionTier, get_tier_config
from app.services.auth import get_password_hash, get_saas_admin_password_hash
from app.services.tokens import generate_registration_code, generate_visitor_code
import uuid

async def seed_data():
    async with SessionLocal() as db:
        # 1. Create Estate
        estate_id = uuid.uuid4()
        app_id = "RP-GREENVIEW"
        estate = Estate(
            id=estate_id,
            app_id=app_id,
            name="Greenview Estate",
            is_active=True
        )
        db.add(estate)
        
        # 2. Create Subscription (Standard Tier: $30/mo, 50k quota, RFID, Real-time alerts, 90-day retention)
        tier_cfg = get_tier_config(SubscriptionTier.STANDARD)
        subscription = Subscription(
            estate_id=estate_id,
            status="active",
            expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date(),
            tier=tier_cfg["tier"],
            monthly_verifications_limit=tier_cfg["monthly_verifications_limit"],
            current_month_verifications=0,
            rfid_enabled=tier_cfg["rfid_enabled"],
            log_retention_days=tier_cfg["log_retention_days"],
            realtime_alerts_enabled=tier_cfg["realtime_alerts_enabled"],
            price_monthly=tier_cfg["price_monthly"]
        )
        db.add(subscription)
        
        # 3. Create Estate Caretaker (Estate Admin)
        estate_admin = User(
            email="admin@greenview.com",
            full_name="Estate Admin",
            hashed_password=get_password_hash("admin123"),
            roles=[UserRole.CARETAKER],
            app_id=app_id
        )
        db.add(estate_admin)

        # 3b. Create SaaS Platform Owner (Global Super Admin)
        saas_owner = User(
            email="owner@residencesaas.com",
            full_name="Platform Super Admin",
            hashed_password=get_saas_admin_password_hash("superadmin123"),
            roles=[UserRole.SAAS_OWNER],
            app_id="GLOBAL"
        )
        db.add(saas_owner)
        
        # 4. Create Landlord
        landlord = User(
            email="landlord@greenview.com",
            full_name="John Landlord",
            hashed_password=get_password_hash("landlord123"),
            roles=[UserRole.LANDLORD],
            app_id=app_id
        )
        db.add(landlord)
        await db.flush()
        
        # 5. Create Registration Token
        reg_token = RegistrationToken(
            code=generate_registration_code(),
            landlord_id=landlord.id,
            app_id=app_id
        )
        db.add(reg_token)
        
        # 6. Create Resident
        resident = User(
            email="resident1@greenview.com",
            full_name="Resident Case",
            hashed_password=get_password_hash("resident123"),
            roles=[UserRole.RESIDENT],
            rfid_tag="ABC123456",
            landlord_id=landlord.id,
            app_id=app_id
        )
        db.add(resident)
        await db.flush()
        
        # 7. Create Visitor Token for Resident
        visitor_token = VisitorToken(
            code="1234",
            visitor_name="John Doe",
            resident_id=resident.id,
            app_id=app_id,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
        )
        db.add(visitor_token)
        
        await db.commit()
        print(f"Seeding complete for app_id: {app_id}")
        print(f"Plan Tier: Standard ($30/mo | 50,000 verifications/mo | RFID Enabled | Real-Time Alerts | 90-Day Retention)")
        print(f"Admin: admin@greenview.com / admin123")
        print(f"Landlord: landlord@greenview.com / landlord123")
        print(f"Resident: resident1@greenview.com / resident123 (RFID: ABC123456)")
        print(f"Visitor Token: 1234")

if __name__ == "__main__":
    asyncio.run(seed_data())
