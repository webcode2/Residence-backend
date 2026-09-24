from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# PgBouncer (transaction pooling) cannot reuse prepared statements across clients.
# Disable asyncpg statement cache whenever we talk through a pooler.
_connect_args: dict = {}
if settings.DB_USE_PGBOUNCER or "pgbouncer" in (settings.DATABASE_URL or "").lower():
    _connect_args["statement_cache_size"] = 0

engine = create_async_engine(
    settings.async_database_url,
    echo=(settings.ENVIRONMENT.lower() == "development"),
    future=True,
    pool_pre_ping=True,
    # Keep app-side pools small; PgBouncer multiplexes onto a smaller Postgres pool.
    # Target: workers * (pool_size + max_overflow) << pgbouncer max_client_conn
    # and pgbouncer default_pool_size << Postgres max_connections.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    connect_args=_connect_args,
)

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
