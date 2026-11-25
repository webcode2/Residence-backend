"""Measure service-to-database and service-to-Redis latency from where it runs.

Run it in the same region as the API / worker (e.g. as a Cloud Run job in us-east5):

    python scripts/latency_probe.py

Postgres: read-only queries. Redis: writes throwaway keys under
`residence:latency_probe:` with a 60 s TTL and deletes them afterwards.
Prints one JSON summary line.
"""
import asyncio
import json
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLES = 30


def summary(samples_ms):
    s = sorted(samples_ms)
    return {"median_ms": round(statistics.median(s), 2), "p90_ms": round(s[int(len(s) * 0.9) - 1], 2),
            "min_ms": round(s[0], 2), "max_ms": round(s[-1], 2), "n": len(s)}


async def timed(fn, n=SAMPLES):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


async def main():
    import asyncpg
    import redis.asyncio as aioredis
    from sqlalchemy import select, text
    from app.core.config import settings
    from app.core.database import SessionLocal, engine
    from app.models.user import User

    result = {"region": os.environ.get("PROBE_REGION", "unknown")}

    # --- Postgres -----------------------------------------------------------
    dsn = settings.async_database_url.replace("postgresql+asyncpg://", "postgresql://")
    t = time.perf_counter()
    conn = await asyncpg.connect(dsn, ssl="require")
    result["pg_new_connection_ms"] = round((time.perf_counter() - t) * 1000, 1)
    result["pg_round_trip_select_1"] = summary(await timed(lambda: conn.fetchval("select 1")))
    await conn.close()

    # App-style: pooled session, one indexed primary-key read (BEGIN/SELECT/ROLLBACK).
    async with SessionLocal() as s:
        user_id = await s.scalar(select(User.id).limit(1))

    async def app_read():
        async with SessionLocal() as s:
            await s.execute(select(User).where(User.id == user_id))
    await app_read()  # warm the pool
    result["pg_app_request_1_indexed_read"] = summary(await timed(app_read))

    async def app_read_3():
        async with SessionLocal() as s:
            for _ in range(3):
                await s.execute(select(User).where(User.id == user_id))
    result["pg_app_request_3_indexed_reads"] = summary(await timed(app_read_3, 15))
    await engine.dispose()

    # --- Redis (visitor-token style write then read) -------------------------
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=3)
    t = time.perf_counter()
    await r.ping()
    result["redis_first_command_incl_tls_ms"] = round((time.perf_counter() - t) * 1000, 1)
    prefix = f"residence:latency_probe:{uuid.uuid4().hex[:8]}"
    token = json.dumps({"id": str(uuid.uuid4()), "code": "ABC123", "status": "active", "app_id": "PROBE"})
    keys = [f"{prefix}:{i}" for i in range(SAMPLES)]
    it = iter(keys)
    result["redis_create_token_SET_EX"] = summary(await timed(lambda: r.set(next(it), token, ex=60)))
    it = iter(keys)
    result["redis_read_token_GET"] = summary(await timed(lambda: r.get(next(it))))
    result["redis_ping"] = summary(await timed(r.ping))
    await r.delete(*keys)
    await r.aclose()

    print("LATENCY_PROBE " + json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
