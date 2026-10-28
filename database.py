import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import DATABASE_URL

# Each uvicorn worker gets its own engine/pool (separate process). Postgres'
# default max_connections is 100, so pool_size × max_overflow × worker_count
# must stay comfortably under that with headroom for migrations/admin tools.
# Defaults below assume ~4 workers (8+4)*4 = 48 total, safely under 100 —
# override via env if the server is tuned for a different worker count or a
# Postgres with a higher max_connections.
_POOL_SIZE = int(os.environ.get("OXYPC_DB_POOL_SIZE", "8"))
_MAX_OVERFLOW = int(os.environ.get("OXYPC_DB_MAX_OVERFLOW", "4"))

# Tests (tests/conftest.py sets OXYPC_DB_NULLPOOL=1): no pooling. asyncpg
# connections belong to the event loop that opened them, and the suite runs
# TestClient (its own loop), asyncio.run() helpers and pytest-asyncio tests
# in one process — a pooled connection handed to another loop fails with
# "attached to a different loop". A fresh connection per session avoids it.
if os.environ.get("OXYPC_DB_NULLPOOL") == "1":
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, echo=False)
else:
    engine = create_async_engine(
        DATABASE_URL,
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=False,
    )

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
