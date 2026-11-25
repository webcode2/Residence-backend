import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.user import User, UserRole
from app.services.auth import verify_saas_admin_password
from app.services.owner_bootstrap import ensure_platform_owner

PASSWORD = "owner-bootstrap-password"


def owner_email():
    return f"owner-{uuid.uuid4().hex[:8]}@example.com"


async def users_with(db, email):
    return (await db.execute(select(User).where(User.email == email).execution_options(populate_existing=True))).scalars().all()


async def test_skips_when_not_configured(db_session):
    assert await ensure_platform_owner(db_session, email="", password="") == "skipped"


async def test_creates_owner_that_can_sign_in(db_session):
    email = owner_email()
    assert await ensure_platform_owner(db_session, email=email.upper(), password=PASSWORD) == "created"
    [user] = await users_with(db_session, email)
    assert user.roles == [UserRole.SAAS_OWNER] and user.app_id == "GLOBAL" and not user.is_revoked
    assert verify_saas_admin_password(PASSWORD, user.hashed_password)


async def test_eight_character_password_is_accepted(db_session):
    email = owner_email()
    assert await ensure_platform_owner(db_session, email=email, password="eight888") == "created"
    [user] = await users_with(db_session, email)
    assert verify_saas_admin_password("eight888", user.hashed_password)


async def test_existing_owner_is_never_overridden(db_session):
    email = owner_email()
    assert await ensure_platform_owner(db_session, email=email, password=PASSWORD, full_name="Original") == "created"
    [before] = await users_with(db_session, email)
    original_hash = before.hashed_password

    assert await ensure_platform_owner(db_session, email=email, password="a-different-password-123", full_name="Changed") == "exists"
    [after] = await users_with(db_session, email)
    assert after.hashed_password == original_hash and after.full_name == "Original"
    assert verify_saas_admin_password(PASSWORD, after.hashed_password)


async def test_estate_account_with_same_email_gets_separate_owner(db_session):
    email = owner_email()
    db_session.add(User(email=email, app_id="RP-ESTATE", roles=[UserRole.CARETAKER], hashed_password="unused"))
    await db_session.commit()
    assert await ensure_platform_owner(db_session, email=email, password=PASSWORD) == "created"
    accounts = {u.app_id: u for u in await users_with(db_session, email)}
    assert accounts["RP-ESTATE"].roles == [UserRole.CARETAKER] and accounts["RP-ESTATE"].hashed_password == "unused"
    assert accounts["GLOBAL"].roles == [UserRole.SAAS_OWNER]
    assert await ensure_platform_owner(db_session, email=email, password="another-password") == "exists"


async def test_global_non_owner_with_same_email_is_not_promoted(db_session):
    email = owner_email()
    db_session.add(User(email=email, app_id="GLOBAL", roles=[UserRole.RESIDENT], hashed_password="unused"))
    await db_session.commit()
    assert await ensure_platform_owner(db_session, email=email, password=PASSWORD) == "exists_not_owner"
    [user] = await users_with(db_session, email)
    assert user.roles == [UserRole.RESIDENT] and user.hashed_password == "unused"


@pytest.mark.parametrize("email,password", [("not-an-email", PASSWORD), (None, "seven77"), (None, "x" * 73)])
async def test_invalid_values_create_nothing(db_session, email, password):
    email = email or owner_email()
    assert await ensure_platform_owner(db_session, email=email, password=password) == "invalid"
    assert await users_with(db_session, email.lower()) == []


async def test_concurrent_starts_create_exactly_one_owner(engine, setup_db):
    email = owner_email()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def start():
        async with factory() as db:
            return await ensure_platform_owner(db, email=email, password=PASSWORD)

    outcomes = await asyncio.gather(*(start() for _ in range(4)))
    assert outcomes.count("created") == 1 and set(outcomes) <= {"created", "exists"}
    async with factory() as db:
        assert len(await users_with(db, email)) == 1


async def test_owner_with_shared_email_signs_in_to_owner_console_only(client, db_session):
    from app.services.auth import get_password_hash
    email = owner_email()
    db_session.add(User(email=email, app_id="RP-ESTATE", roles=[UserRole.CARETAKER],
                        hashed_password=get_password_hash("estate-password-123")))
    await db_session.commit()
    assert await ensure_platform_owner(db_session, email=email, password=PASSWORD) == "created"
    ok = await client.post("/api/v1/admin/auth/login", json={"email": email.upper(), "password": PASSWORD})
    assert ok.status_code == 200, ok.text
    wrong = await client.post("/api/v1/admin/auth/login", json={"email": email, "password": "estate-password-123"})
    assert wrong.status_code == 401
