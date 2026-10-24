import pytest
import asyncio
from typing import AsyncGenerator
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.main import app
from app.core.database import Base, get_db
from app.core.config import settings

# Use a test-specific database via direct connection (bypasses PgBouncer pool mapping)
TEST_DATABASE_URL = (settings.async_direct_database_url or settings.async_database_url) + "_test"

@pytest.fixture
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, future=True, echo=False)
    yield engine
    await engine.dispose()

@pytest.fixture
async def setup_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest.fixture
async def db_session(setup_db, engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session
        await session.rollback()

@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client
    app.dependency_overrides.clear()

@pytest.fixture
async def guard_headers(db_session):
    """Create an authenticated guard in the requested isolated test estate."""
    import uuid
    from app.models.user import User, UserRole
    from app.services.auth import create_access_token, get_password_hash
    async def create(app_id):
        guard = User(email=uuid.uuid4().hex + "@guard.example", app_id=app_id,
                     roles=[UserRole.SECURITY], hashed_password=get_password_hash("guard-test-password"))
        db_session.add(guard)
        await db_session.commit()
        token = create_access_token({"sub": guard.email, "app_id": app_id, "roles": ["security"]})
        return {"X-App-Id": app_id, "Authorization": "Bearer " + token}
    return create
