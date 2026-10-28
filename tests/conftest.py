# tests/conftest.py
import asyncio
import os

# Before anything imports database.py: no connection pool under test (see
# database.py) — pooled asyncpg connections can't cross event loops.
os.environ.setdefault("OXYPC_DB_NULLPOOL", "1")
import pytest
import pytest_asyncio


@pytest.fixture()
def app_client():
    """TestClient bound to the real app, with the lifespan actually run.

    Entered as a context manager on purpose: bare TestClient(app) skips
    startup/shutdown, so the caches main.py builds on startup stay empty and
    routes behave differently under test than in production.
    """
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        yield client


@pytest_asyncio.fixture()
async def db():
    """An AsyncSession against the configured database.

    The engine is disposed on the way in and out. asyncpg connections are bound
    to the event loop that opened them, and this suite also runs TestClient
    tests that spin up their own loop — a pooled connection carried across that
    boundary fails with a bare "'NoneType' object has no attribute 'send'" that
    looks like a database fault and is not one. Disposing costs one reconnect
    per test and removes the whole class of confusion.
    """
    from database import AsyncSessionLocal, engine

    await engine.dispose()
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        # Roll back rather than commit: a test that writes must not leave rows
        # behind for the next test to trip over.
        await session.rollback()
        await session.close()
        await engine.dispose()


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop so async tests share one loop."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    try:
        loop.stop()
        loop.close()
    except RuntimeError:
        pass
