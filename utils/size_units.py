"""RAM / Storage size spelling (2026-10-19): a space between the number and
the unit — "12GB" -> "12 GB", "1TB" -> "1 TB". Applied to new Dropdown
Configuration values as they are saved, and once on server start to the values
already in the database (Product Master dropdowns, Hardware RAM / Storage rows,
saved product picks, Pricing Calculator rows and Master Data RAM / Storage
Sizes)."""
import re

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(gb|tb|mb)\s*$", re.IGNORECASE)

# Product Master Dropdown Configuration and Admin Master Data categories
PM_SIZE_CATEGORIES = ("ram_capacity", "storage_capacity")
MASTER_SIZE_CATEGORIES = ("ram_size", "storage_size")


def space_size_unit(value):
    """'12GB' / '12 gb' / ' 12GB ' -> '12 GB'. Anything that is not just a
    number followed by GB / TB / MB is returned unchanged."""
    if value is None:
        return None
    m = _SIZE.match(value)
    return f"{m.group(1)} {m.group(2).upper()}" if m else value


def _spaced_name(name: str) -> str:
    """'DDR4/12GB' -> 'DDR4/12 GB' (Hardware row names are '<Type>/<Size>')."""
    if not name or "/" not in name:
        return name
    head, tail = name.rsplit("/", 1)
    return f"{head}/{space_size_unit(tail)}"


async def space_existing_size_values(db) -> dict:
    """Rewrite stored RAM / Storage sizes to the spaced spelling. Idempotent:
    values already spaced are left alone. A rename that would collide with an
    existing spaced value (same category, or same Hardware name) is skipped,
    so no row is merged or deleted. Returns counts of rows changed."""
    from sqlalchemy import select
    from models.product_master import PMDropdownValue, HWRam, HWStorage, PMProduct, PMCalcRow
    from models.master import MasterData

    counts = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    # Product Master -> Dropdown Configuration (unique per category + parent + value)
    rows = (await db.execute(select(PMDropdownValue).where(
        PMDropdownValue.category.in_(PM_SIZE_CATEGORIES)))).scalars().all()
    taken = {(r.category, r.parent_id, r.value) for r in rows}
    for r in rows:
        new = space_size_unit(r.value)
        if new != r.value and (r.category, r.parent_id, new) not in taken:
            taken.discard((r.category, r.parent_id, r.value))
            taken.add((r.category, r.parent_id, new))
            r.value = new
            bump("dropdown")

    # Hardware RAM / Storage rows (name is unique)
    for Model, cap, name, key in ((HWRam, "ram_capacity", "ram_name", "hw_ram"),
                                  (HWStorage, "storage_capacity", "storage_name", "hw_storage")):
        rows = (await db.execute(select(Model))).scalars().all()
        names = {getattr(r, name).lower() for r in rows}
        for r in rows:
            new_cap = space_size_unit(getattr(r, cap))
            new_name = _spaced_name(getattr(r, name))
            if new_cap == getattr(r, cap) and new_name == getattr(r, name):
                continue
            if new_name.lower() != getattr(r, name).lower() and new_name.lower() in names:
                continue
            names.discard(getattr(r, name).lower())
            names.add(new_name.lower())
            setattr(r, cap, new_cap)
            setattr(r, name, new_name)
            bump(key)

    # Saved product picks and Pricing Calculator rows (plain text, no constraint)
    for Model, key in ((PMProduct, "products"), (PMCalcRow, "calc_rows")):
        for r in (await db.execute(select(Model))).scalars():
            changed = False
            for col in ("ram_size", "storage_size"):
                old = getattr(r, col)
                new = space_size_unit(old)
                if new != old:
                    setattr(r, col, new)
                    changed = True
            if changed:
                bump(key)

    # Admin -> Master Data -> RAM Sizes / Storage Sizes
    rows = (await db.execute(select(MasterData).where(
        MasterData.category.in_(MASTER_SIZE_CATEGORIES)))).scalars().all()
    taken = {(r.category, r.value.lower()) for r in rows}
    for r in rows:
        new = space_size_unit(r.value)
        if new != r.value and (r.category, new.lower()) not in taken:
            taken.discard((r.category, r.value.lower()))
            taken.add((r.category, new.lower()))
            r.value = new
            bump("master_data")

    if counts:
        await db.commit()
    return counts
