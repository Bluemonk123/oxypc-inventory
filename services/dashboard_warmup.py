"""Keep the Main Dashboard cache warm (2026-11-15, Pankaj).

The dashboard's sections are cached in memory (routers/dashboard.py, 30 s
stale-while-revalidate), so every load after the first is fast — but the very
first load after a server start counted everything while the user waited.
This loads the default dashboard (default Entity, no other filters) inside the
app shortly after start-up, then every 10 minutes, as an active admin, so the
cache is already filled when anyone signs in. No network, no browser, nothing
written; a failure only prints a line and is tried again next time.
"""
import asyncio

INTERVAL = 600      # seconds between warm-ups
FIRST_DELAY = 15    # let start-up finish first


async def _warm_once(app):
    import httpx
    from sqlalchemy import select
    from auth.dependencies import create_access_token
    from database import AsyncSessionLocal
    from models.user import User
    async with AsyncSessionLocal() as db:
        admin = (await db.execute(select(User).where(User.status == True)  # noqa: E712
                                  .where(User.role == "admin").limit(1))).scalar_one_or_none()
    if not admin:
        return None
    token = create_access_token({"sub": admin.username, "role": "admin"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://warmup.local", timeout=300) as c:
        r = await c.get("/dashboard", params={"_part": "1"}, cookies={"access_token": token},
                        headers={"X-Dashboard-Warmup": "1"})
    return r.status_code


async def loop(app):
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            import time
            t0 = time.perf_counter()
            code = await _warm_once(app)
            print(f"  [DashWarmup] Dashboard cache warmed ({code}) in {time.perf_counter() - t0:.1f}s")
        except Exception as e:             # never break the app
            print(f"  [DashWarmup] Warm-up failed (will retry): {e}")
        await asyncio.sleep(INTERVAL)
