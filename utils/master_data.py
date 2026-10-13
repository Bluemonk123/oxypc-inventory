"""Shared helper for fetching admin-managed Master Data dropdown options.

Two ways to use this in a route/template:

1. Router-side, when you need the list in Python (e.g. to validate a submitted
   value): `await master_values(db, "payment_mode")`.
2. Template-side, via the Jinja global `master_options('payment_mode')` —
   backed by an in-memory cache (mirrors the role-permissions cache pattern in
   models/role_permissions.py) so templates can pull dropdown options directly
   without every router call site having to fetch and pass them explicitly.
   Warmed at startup and refreshed whenever admin adds/edits/toggles/deletes
   a value on /admin/master.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.master import MasterData

# category -> [values...], active only, in display order. See module docstring.
_MASTER_CACHE: dict = {}


async def master_values(db: AsyncSession, category: str) -> list:
    """Return active option values for a Master Data category, in display order."""
    result = await db.execute(
        select(MasterData.value)
        .where(MasterData.category == category, MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )
    return [row[0] for row in result.all()]


# Used when the 'entity' Master Data category has not been populated on a given
# database — without it, an unconfigured install shows an empty Entity dropdown
# and rejects every entity on bulk upload.
ENTITY_FALLBACK = ["Deshwal", "OxyPC Computers", "Renew Circuits"]


async def entity_values(db: AsyncSession) -> list:
    """Active Entity options, falling back to the three known entities when the
    category has not been configured. Single source of truth for the Entity
    dropdown on Entity Movement, the All Inventory filter, and bulk-upload
    validation — these previously each carried their own copy of the fallback
    list, so adding a fourth entity meant finding every one of them."""
    return await master_values(db, "entity") or list(ENTITY_FALLBACK)


async def report_year_values(db: AsyncSession) -> list:
    """Active Year options for the Dashboard's Year filter and Business P&L's
    year tabs — single source so the two never disagree on which years are
    offered. Falls back to a rolling 5-year window (current year ± 2) when the
    'report_year' category hasn't been configured yet, same fallback pattern
    as entity_values() above."""
    vals = await master_values(db, "report_year")
    if vals:
        return vals
    from utils.timezone import app_now
    y = app_now().year
    return [str(v) for v in range(y - 2, y + 3)]


# Categories whose MASTER_SEED values are written into an existing database on
# server start, but only while the category has no rows at all (active or not).
# MASTER_SEED otherwise seeds brand-new installs only, so a category added later
# would ship with an empty dropdown everywhere until an admin typed every value.
ENSURE_SEEDED_CATEGORIES = ("dealer_state", "storage_size", "ram_size")


async def ensure_master_defaults(db: AsyncSession, categories=ENSURE_SEEDED_CATEGORIES) -> int:
    """Insert MASTER_SEED values for each category that has no rows yet.
    Returns how many values were inserted. Never touches a category an admin
    has already populated (or emptied by deactivating values)."""
    from sqlalchemy import func
    from models.master import MASTER_SEED
    added = 0
    for cat in categories:
        seed = MASTER_SEED.get(cat) or []
        if not seed:
            continue
        existing = (await db.execute(
            select(func.count()).select_from(MasterData).where(MasterData.category == cat)
        )).scalar() or 0
        if existing:
            continue
        for i, value in enumerate(seed):
            db.add(MasterData(category=cat, value=value, display_order=i))
            added += 1
    if added:
        await db.commit()
    return added


async def add_missing_master_values(db: AsyncSession, category: str, values, reactivate: bool = False) -> list:
    """Add each value not already in the category (compared case-insensitively,
    active or not) to the end of its display order. With reactivate=True a value
    that exists but was switched off is switched back on, so it shows in the
    dropdown again. Returns the values added or switched back on.
    Does not commit; the caller commits with its own changes."""
    rows = (await db.execute(
        select(MasterData).where(MasterData.category == category)
    )).scalars().all()
    have = {(r.value or "").strip().lower(): r for r in rows}
    order = max([r.display_order or 0 for r in rows], default=-1)
    added = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        existing = have.get(v.lower())
        if existing is not None:
            if reactivate and existing.is_active is False:
                existing.is_active = True
                added.append(existing.value)
            continue
        order += 1
        row = MasterData(category=category, value=v, display_order=order)
        db.add(row)
        have[v.lower()] = row
        added.append(v)
    return added


async def refresh_master_cache(db: AsyncSession) -> None:
    """Reload the full in-memory cache from DB. Call at startup and after any
    admin add/edit/toggle/delete on /admin/master."""
    result = await db.execute(
        select(MasterData.category, MasterData.value)
        .where(MasterData.is_active == True)
        .order_by(MasterData.category, MasterData.display_order, MasterData.value)
    )
    fresh: dict = {}
    for category, value in result.all():
        fresh.setdefault(category, []).append(value)
    _MASTER_CACHE.clear()
    _MASTER_CACHE.update(fresh)


def master_options(category: str) -> list:
    """Synchronous Jinja-global accessor — reads the warm cache, never hits the DB."""
    return _MASTER_CACHE.get(category, [])
