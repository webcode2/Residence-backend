#!/bin/sh
set -e

echo "[ENTRYPOINT] Checking database and redis connectivity..."

# Python helper to verify PostgreSQL and Redis connectivity
python3 - << 'EOF'
import sys
import time
import os
import asyncio
from urllib.parse import urlparse

async def wait_for_services():
    from app.core.config import settings
    from app.core.database import engine
    from sqlalchemy import text
    import redis.asyncio as aioredis

    max_retries = 30
    retry_interval = 2

    # 1. Wait for Database
    print("[ENTRYPOINT] Connecting to PostgreSQL...")
    for i in range(max_retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print("[ENTRYPOINT] PostgreSQL is ready and accepting connections.")
            break
        except Exception as e:
            if i == max_retries - 1:
                print(f"[ENTRYPOINT] Could not connect to PostgreSQL after {max_retries} attempts: {e}")
                sys.exit(1)
            print(f"[ENTRYPOINT] Waiting for PostgreSQL ({i+1}/{max_retries})...")
            time.sleep(retry_interval)

    # 2. Wait for Redis
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

# Run database migrations unless explicitly skipped
if [ "$SKIP_MIGRATIONS" != "1" ]; then
    echo "[ENTRYPOINT] Running database migrations (alembic upgrade head)..."
    alembic upgrade head
    echo "[ENTRYPOINT] Database migrations successfully applied."
else
    echo "[ENTRYPOINT] Skipping database migrations as SKIP_MIGRATIONS=1."
fi

# Execute CMD with exec to ensure proper PID 1 signal forwarding
exec "$@"
