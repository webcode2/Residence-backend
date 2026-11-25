import asyncio
import logging
import time

from sqlalchemy import event, exc, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# PgBouncer (transaction pooling) cannot reuse prepared statements across clients.
# Disable asyncpg statement cache whenever we talk through a pooler.
_connect_args: dict = {}
_configured_db_url = (settings.DB_URL or settings.DATABASE_URL or "").lower()
if settings.DB_USE_PGBOUNCER or "pgbouncer" in _configured_db_url:
    _connect_args["statement_cache_size"] = 0

engine = create_async_engine(
    settings.async_database_url,
    echo=(settings.ENVIRONMENT.lower() == "development"),
    future=True,
    # Replaced by the idle-aware ping below; see DB_POOL_PING_IDLE_SECONDS.
    pool_pre_ping=False,
    # Keep app-side pools small; PgBouncer multiplexes onto a smaller Postgres pool.
    # Target: workers * (pool_size + max_overflow) << pgbouncer max_client_conn
    # and pgbouncer default_pool_size << Postgres max_connections.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    connect_args=_connect_args,
)

logger = logging.getLogger(__name__)
_LAST_USED = "residence_last_used"


@event.listens_for(engine.sync_engine, "connect")
def _mark_new_connection(dbapi_connection, connection_record):
    connection_record.info[_LAST_USED] = time.monotonic()


@event.listens_for(engine.sync_engine, "checkin")
def _mark_returned_connection(dbapi_connection, connection_record):
    if connection_record is not None:
        connection_record.info[_LAST_USED] = time.monotonic()


@event.listens_for(engine.sync_engine, "checkout")
def _ping_if_idle(dbapi_connection, connection_record, connection_proxy):
    """Ping only connections that sat idle long enough to have been dropped
    (e.g. Neon compute suspend). Busy connections skip the ping entirely.
    Raising DisconnectionError makes the pool discard it and retry with a new one."""
    idle = time.monotonic() - connection_record.info.get(_LAST_USED, 0.0)
    if idle < settings.DB_POOL_PING_IDLE_SECONDS:
        return
    try:
        dbapi_connection.ping()
    except Exception as error:
        raise exc.DisconnectionError(f"stale pooled connection after {idle:.0f}s idle") from error


async def warm_pool(connections: int | None = None, timeout: float = 20.0) -> None:
    """Open pooled connections at startup so user requests do not pay the handshake."""
    count = min(settings.DB_POOL_WARM_CONNECTIONS if connections is None else connections,
                settings.DB_POOL_SIZE)
    if count <= 0:
        return

    async def _open():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            results = await asyncio.gather(*(_open() for _ in range(count)), return_exceptions=True)
    except TimeoutError:
        logger.warning("db_pool_warm timed out after %.0fs", timeout)
        return
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        logger.warning("db_pool_warm opened %d/%d connections: %s", count - len(failures), count, failures[0])
    else:
        logger.info("db_pool_warm opened %d connections in %.0f ms", count, (time.perf_counter() - started) * 1000)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
