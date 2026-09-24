"""In-container capacity probe for visitor token CREATE (service layer)."""
import asyncio
import time
from sqlalchemy import select
from app.core.database import SessionLocal, engine
from app.core.redis import close_redis
from app.services import tokens as token_service
from app.schemas.token import VisitorTokenCreateSchema
from app.models.user import User

APP = "RP-GREENVIEW"
RESIDENT_EMAIL = "resident1@greenview.com"
OUT = "/tmp/verify_codes.txt"


def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return vals[f] if f == c else vals[f] + (vals[c] - vals[f]) * (k - f)


async def get_resident_id():
    async with SessionLocal() as db:
        r = await db.execute(select(User).where(User.email == RESIDENT_EMAIL, User.app_id == APP))
        user = r.scalar_one()
        return user.id


async def create_one(resident_id):
    t0 = time.perf_counter()
    try:
        async with SessionLocal() as db:
            tok = await token_service.create_visitor_token(
                db, VisitorTokenCreateSchema(visitor_name="Cap"), resident_id, APP
            )
            return True, (time.perf_counter() - t0) * 1000, tok.code
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000, str(e)


async def run_create(n, concurrency, resident_id):
    sem = asyncio.Semaphore(concurrency)
    results = []

    async def wrapped():
        async with sem:
            results.append(await create_one(resident_id))

    t0 = time.perf_counter()
    await asyncio.gather(*[wrapped() for _ in range(n)])
    wall = time.perf_counter() - t0
    ok = [r for r in results if r[0]]
    lats = [r[1] for r in results]
    print(f"\n### CREATE service-layer n={n} c={concurrency}")
    print(f"  success={len(ok)}/{n} fail={n - len(ok)}")
    print(f"  throughput={n / wall:,.1f} req/s  wall={wall:.2f}s")
    print(f"  latency p50={pct(lats, 50):.0f} p95={pct(lats, 95):.0f} p99={pct(lats, 99):.0f} max={max(lats):.0f} ms")
    if n - len(ok):
        print(f"  sample_error={next(r[2] for r in results if not r[0])[:200]}")
    return [r[2] for r in ok]


async def main():
    resident_id = await get_resident_id()
    all_codes = []
    for n, c in [(100, 10), (200, 25), (300, 40), (400, 60), (500, 80)]:
        all_codes.extend(await run_create(n, c, resident_id))
    # bank extra for HTTP verify
    all_codes.extend(await run_create(1000, 50, resident_id))
    with open(OUT, "w") as f:
        f.write("\n".join(all_codes))
    print(f"\nTOTAL_CODES={len(all_codes)} wrote {OUT}")
    await close_redis()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
