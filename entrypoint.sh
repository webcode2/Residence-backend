#!/bin/sh
set -e

echo "[ENTRYPOINT] Checking database and redis connectivity..."

# Python helper to verify PostgreSQL (direct), PgBouncer (optional), and Redis
python3 - << 'EOF'
import sys
import time
import asyncio

async def wait_for_services():
    from app.core.config import settings
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    import redis.asyncio as aioredis

    max_retries = 30
    retry_interval = 2

    direct_url = settings.async_direct_database_url
    app_url = settings.async_database_url

    # 1. Wait for Postgres directly (required for migrations)
    print("[ENTRYPOINT] Connecting to PostgreSQL (direct)...")
    direct_engine = create_async_engine(direct_url, pool_pre_ping=True)
    try:
        for i in range(max_retries):
            try:
                async with direct_engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                print("[ENTRYPOINT] PostgreSQL is ready and accepting connections.")
                break
            except Exception as e:
                if i == max_retries - 1:
                    print(f"[ENTRYPOINT] Could not connect to PostgreSQL after {max_retries} attempts: {e}")
                    sys.exit(1)
                print(f"[ENTRYPOINT] Waiting for PostgreSQL ({i+1}/{max_retries})...")
                time.sleep(retry_interval)
    finally:
        await direct_engine.dispose()

    # 2. Wait for PgBouncer when configured (app traffic path)
    if settings.DB_USE_PGBOUNCER or "pgbouncer" in (settings.DATABASE_URL or "").lower():
        print("[ENTRYPOINT] Connecting via PgBouncer...")
        pooler_engine = create_async_engine(
            app_url,
            pool_pre_ping=True,
            connect_args={"statement_cache_size": 0},
        )
        try:
            for i in range(max_retries):
                try:
                    async with pooler_engine.connect() as conn:
                        await conn.execute(text("SELECT 1"))
                    print("[ENTRYPOINT] PgBouncer is ready and accepting connections.")
                    break
                except Exception as e:
                    if i == max_retries - 1:
                        print(f"[ENTRYPOINT] Could not connect to PgBouncer after {max_retries} attempts: {e}")
                        sys.exit(1)
                    print(f"[ENTRYPOINT] Waiting for PgBouncer ({i+1}/{max_retries})...")
                    time.sleep(retry_interval)
        finally:
            await pooler_engine.dispose()

    # 3. Wait for Redis
    print("[ENTRYPOINT] Connecting to Redis...")
    for i in range(max_retries):
        try:
            client = aioredis.from_url(settings.REDIS_URL, socket_timeout=3)
            await client.ping()
            await client.aclose()
            print("[ENTRYPOINT] Redis is ready and accepting commands.")
            break
        except Exception as e:
            if i == max_retries - 1:
                print(f"[ENTRYPOINT] Could not connect to Redis after {max_retries} attempts: {e}")
                sys.exit(1)
            print(f"[ENTRYPOINT] Waiting for Redis ({i+1}/{max_retries})...")
            time.sleep(retry_interval)

if __name__ == "__main__":
    asyncio.run(wait_for_services())
EOF

# Run database migrations against DIRECT Postgres (bypass transaction pooler)
if [ "$SKIP_MIGRATIONS" != "1" ]; then
    echo "[ENTRYPOINT] Running database migrations (alembic upgrade head)..."
    alembic upgrade head
    echo "[ENTRYPOINT] Database migrations successfully applied."
else
    echo "[ENTRYPOINT] Skipping database migrations as SKIP_MIGRATIONS=1."
fi

# Execute CMD with exec to ensure proper PID 1 signal forwarding
exec "$@"
