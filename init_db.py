import asyncio
from app.core.database import engine, Base
# Import all models to register them with Base
from app.models.estate import Estate, Subscription
from app.models.user import User
from app.models.token import RegistrationToken, VisitorToken
from app.models.access_log import AccessLog

async def init_db():
    async with engine.begin() as conn:
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created successfully.")

if __name__ == "__main__":
    asyncio.run(init_db())
