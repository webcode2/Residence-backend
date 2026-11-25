"""Create the platform owner (SaaS admin) account on startup if it is missing.

Runs from entrypoint.sh after migrations on every container start (Cloud Run and
docker-compose). It never modifies an existing account: no password reset, no role
change. Estate logins sharing the email are separate accounts and stay untouched.
Concurrent starts are safe because the insert is ON CONFLICT DO NOTHING on the
unique (app_id, email) index.

    python -m app.services.owner_bootstrap
"""
import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.models.user import User, UserRole
from app.services.auth import get_saas_admin_password_hash_async

logger = logging.getLogger("owner_bootstrap")

OWNER_APP_ID = "GLOBAL"  # matches seed.py; owner accounts are not tied to an estate
OWNER_PASSWORD_MIN_LENGTH = 8  # also enforced by AdminChangePasswordSchema


async def ensure_platform_owner(db, email: str | None = None, password: str | None = None,
                                full_name: str | None = None) -> str:
    """Return one of: skipped, invalid, exists, exists_not_owner, created."""
    email = (settings.PLATFORM_OWNER_EMAIL if email is None else email).strip().lower()
    password = settings.PLATFORM_OWNER_PASSWORD if password is None else password
    full_name = (full_name or settings.PLATFORM_OWNER_NAME or "Residence Platform Owner").strip()

    if not email or not password:
        logger.info("platform_owner skipped: PLATFORM_OWNER_EMAIL/PLATFORM_OWNER_PASSWORD not set")
        return "skipped"
    if "@" not in email:
        logger.error("platform_owner invalid: PLATFORM_OWNER_EMAIL is not an email address")
        return "invalid"
    if len(password) < OWNER_PASSWORD_MIN_LENGTH or len(password.encode("utf-8")) > 72:
        logger.error("platform_owner invalid: PLATFORM_OWNER_PASSWORD must be %d+ characters and at most 72 UTF-8 bytes",
                     OWNER_PASSWORD_MIN_LENGTH)
        return "invalid"

    if await _owner_exists(db, email):
        logger.info("platform_owner exists: %s (left unchanged)", email)
        return "exists"
    # The same person may also hold estate logins under this email; those are separate
    # accounts (unique per app_id) and are never modified or promoted here.

    hashed = await get_saas_admin_password_hash_async(password)
    result = await db.execute(
        insert(User.__table__)
        .values(email=email, full_name=full_name, hashed_password=hashed,
                roles=[UserRole.SAAS_OWNER], app_id=OWNER_APP_ID, is_revoked=False,
                must_change_password=False)
        .on_conflict_do_nothing(index_elements=["app_id", "email"])
    )
    await db.commit()
    if result.rowcount:
        logger.info("platform_owner created: %s", email)
        return "created"
    if await _owner_exists(db, email):
        logger.info("platform_owner exists: %s (created concurrently, left unchanged)", email)
        return "exists"
    logger.warning("platform_owner exists_not_owner: a non-owner %s account already uses %s; "
                   "not promoting it. Use a dedicated owner email.", OWNER_APP_ID, email)
    return "exists_not_owner"


async def _owner_exists(db, email: str) -> bool:
    return (await db.execute(select(User.id).where(
        User.email == email, User.roles.contains([UserRole.SAAS_OWNER])).limit(1))).first() is not None


async def _main() -> int:
    from app.core.database import SessionLocal, engine
    try:
        async with SessionLocal() as db:
            outcome = await ensure_platform_owner(db)
    except Exception:
        logger.exception("platform_owner failed")
        return 1
    finally:
        await engine.dispose()
    print(f"[ENTRYPOINT] Platform owner: {outcome}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[ENTRYPOINT] %(message)s")
    sys.exit(asyncio.run(_main()))
