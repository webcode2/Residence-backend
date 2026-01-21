import asyncio
from datetime import datetime, timedelta, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import SessionLocal, engine
from app.models.estate import Estate, Subscription
from app.models.user import User, UserRole
from app.models.token import RegistrationToken, VisitorToken
from app.services.auth import get_password_hash
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
        
        # 2. Create Subscription
        subscription = Subscription(
            estate_id=estate_id,
            status="active",
        expiry_date=(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=365)).date()
        )
        db.add(subscription)
        
        # 3. Create SaaS Owner (Global context for this tenant)
        saas_owner = User(
            email="admin@greenview.com",
            full_name="Estate Admin",
            hashed_password=get_password_hash("admin123"),
            roles=[UserRole.CARETAKER], # Estate Admin
            app_id=app_id
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
        print(f"Admin: admin@greenview.com / admin123")
        print(f"Landlord: landlord@greenview.com / landlord123")
        print(f"Resident: resident1@greenview.com / resident123 (RFID: ABC123456)")
        print(f"Visitor Token: 1234")

if __name__ == "__main__":
    asyncio.run(seed_data())
