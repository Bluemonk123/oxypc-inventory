"""Product Master / Hardware / Spare Parts / Product Pricing (2026-10-14).

Schema approved via dbdiagram.io (product_master_schema.dbml):
  pm_dropdown_values — the 9 Dropdown Configuration lists. Model rows point
                       at their Make (parent_id); CPU Gen rows point at their
                       CPU and store value "<CPU>/<Gen>".
  hw_ram / hw_storage — Hardware page tables; name = "<Category>/<Capacity>".
  pm_products        — one row per product. RAM / Storage prices are read
                       live from hw_ram / hw_storage; Product Price comes from
                       the Product Pricing page and the 8 part prices from the
                       Spare Parts page. Base Price, Discount Amount and
                       Selling Price are computed (see product_figures), never
                       stored, so they cannot go stale.
"""
import uuid
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Column, String, Integer, Numeric, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from database import Base
from utils.timezone import app_now


# category key -> label, in the order the Dropdown Configuration tab shows them
DROPDOWN_CATEGORIES = [
    ("make", "Make"),
    ("model", "Model"),
    ("cpu", "CPU"),
    ("cpu_gen", "CPU Gen"),
    ("ram_category", "RAM Types"),
    ("ram_capacity", "RAM Sizes"),
    ("storage_category", "Storage Types"),
    ("storage_capacity", "Storage Sizes"),
    ("discount_pct", "Discount %"),
]
DROPDOWN_LABELS = dict(DROPDOWN_CATEGORIES)
# child category -> parent category it is mapped to
DROPDOWN_PARENT = {"model": "make", "cpu_gen": "cpu"}

# (column, label) for the 8 spare-part prices, in table order
PART_FIELDS = [
    ("bezel_price", "Bazel"),
    ("screen_price", "Screen"),
    ("hinge_price", "Hinge"),
    ("hinge_cover_price", "Hinge Cover"),
    ("touchpad_price", "Touch Pad"),
    ("base_part_price", "Base"),
    ("keyboard_price", "Keyboard"),
    ("battery_price", "Battery"),
]


class PMDropdownValue(Base):
    __tablename__ = "pm_dropdown_values"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(String(30), nullable=False, index=True)
    value = Column(String(100), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("pm_dropdown_values.id"), nullable=True)
    display_order = Column(Integer, default=0)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    __table_args__ = (
        UniqueConstraint("category", "parent_id", "value", name="uq_pm_dropdown_cat_parent_value"),
    )


class HWRam(Base):
    __tablename__ = "hw_ram"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ram_category = Column(String(100), nullable=False)
    ram_capacity = Column(String(100), nullable=False)
    ram_name = Column(String(210), nullable=False, unique=True)
    ram_price = Column(Numeric(12, 2), nullable=False)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)


class HWStorage(Base):
    __tablename__ = "hw_storage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    storage_category = Column(String(100), nullable=False)
    storage_capacity = Column(String(100), nullable=False)
    storage_name = Column(String(210), nullable=False, unique=True)
    storage_price = Column(Numeric(12, 2), nullable=False)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)


class PMProduct(Base):
    __tablename__ = "pm_products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_name = Column(String(400), nullable=False, unique=True)
    make = Column(String(100), nullable=False)
    model = Column(String(100), nullable=False)
    cpu = Column(String(100), nullable=False)
    cpu_gen = Column(String(210), nullable=False)
    # Optional since v2 (2026-10-14): picked on the Product Pricing page; null = Pending
    ram_id = Column(UUID(as_uuid=True), ForeignKey("hw_ram.id"), nullable=True)
    storage_id = Column(UUID(as_uuid=True), ForeignKey("hw_storage.id"), nullable=True)
    # Schema v4 (approved 2026-10-18): the RAM / Storage Type and Size picked on
    # Product Pricing, saved even before Hardware has a price for them. The
    # price is looked up from Hardware by "<Type>/<Size>" (ram_id / storage_id
    # are linked once that Hardware row exists).
    ram_type = Column(String(100), nullable=True)
    ram_size = Column(String(100), nullable=True)
    storage_type = Column(String(100), nullable=True)
    storage_size = Column(String(100), nullable=True)
    # Schema v5 (approved 2026-10-18): short unique 12-digit SKU, used by the
    # Pricing Calculator upload to find the product.
    sku = Column(String(12), nullable=True, unique=True, index=True)
    product_price = Column(Numeric(12, 2), nullable=True)
    bezel_price = Column(Numeric(12, 2), nullable=True)
    screen_price = Column(Numeric(12, 2), nullable=True)
    hinge_price = Column(Numeric(12, 2), nullable=True)
    hinge_cover_price = Column(Numeric(12, 2), nullable=True)
    touchpad_price = Column(Numeric(12, 2), nullable=True)
    base_part_price = Column(Numeric(12, 2), nullable=True)
    keyboard_price = Column(Numeric(12, 2), nullable=True)
    battery_price = Column(Numeric(12, 2), nullable=True)
    discount_pct = Column(Numeric(5, 2), nullable=True)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)


def new_sku(taken: set) -> str:
    """A random 12-digit SKU (no leading zero) not in `taken`; adds it there."""
    import secrets
    while True:
        v = str(secrets.randbelow(9 * 10**11) + 10**11)
        if v not in taken:
            taken.add(v)
            return v


def build_product_name(make, model, cpu, cpu_gen) -> str:
    """Stored, unique product key (v2): Make/Model/CPU/CPU Gen. The tables
    display Make/Model/CPU Gen."""
    return "/".join([make, model, cpu, cpu_gen])


def parts_done(p: PMProduct) -> bool:
    return all(getattr(p, col) is not None for col, _ in PART_FIELDS)


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def product_figures(p: PMProduct, ram_price, storage_price) -> dict:
    """Server-side pricing for one product (Decimal throughout).
    Base Price    = Product Price - RAM Price - Storage Price
    Discount Amt  = Product Price x Discount% / 100 (0 while Product Price is pending)
    Selling Price = (Base Price - Discount Amt) + (RAM Price + Storage Price)
    Base / Selling are None while Product Price, RAM or Storage is pending."""
    ram = Decimal(ram_price or 0)
    sto = Decimal(storage_price or 0)
    pct = Decimal(p.discount_pct or 0)
    if p.product_price is None:
        return {"base_price": None, "discount_amount": Decimal("0.00"), "selling_price": None}
    if ram_price is None or storage_price is None:
        disc = _q(Decimal(p.product_price) * pct / Decimal(100))
        return {"base_price": None, "discount_amount": disc, "selling_price": None}
    price = Decimal(p.product_price)
    base = price - ram - sto
    disc = _q(price * pct / Decimal(100))
    return {"base_price": _q(base), "discount_amount": disc,
            "selling_price": _q((base - disc) + (ram + sto))}


async def seed_pm_dropdowns_if_empty(db) -> int:
    """One-time backfill of the Dropdown Configuration values entered on
    staging (data/pm_dropdown_seed.json, 2026-10-14). Runs only while
    pm_dropdown_values is completely empty, so it never re-adds a value an
    admin later deletes and never touches existing data. Model / CPU Gen rows
    are linked to their Make / CPU by value. Returns rows inserted."""
    import json, os
    from sqlalchemy import select, func
    if (await db.execute(select(func.count()).select_from(PMDropdownValue))).scalar():
        return 0
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pm_dropdown_seed.json")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    ids, order, added = {}, {}, 0
    for it in items:                       # parents (make, cpu) come before children in the file
        cat, val = it["category"], it["value"]
        parent_id = None
        if it.get("parent"):
            parent_id = ids.get((DROPDOWN_PARENT.get(cat), it["parent"]))
            if parent_id is None:
                continue
        row = PMDropdownValue(id=uuid.uuid4(), category=cat, value=val, parent_id=parent_id,
                              display_order=order.get(cat, 0), created_by="seed")
        order[cat] = order.get(cat, 0) + 1
        db.add(row)
        ids[(cat, val)] = row.id
        added += 1
    await db.commit()
    return added


async def migrate_pm_products_v2(db) -> None:
    """Schema v2 (dbdiagram approved 2026-10-14): ram_id / storage_id become
    nullable and product_name is rebuilt as Make/Model/CPU/CPU Gen. Idempotent;
    a rename that would collide with another product is skipped."""
    from sqlalchemy import text, select
    await db.execute(text("ALTER TABLE pm_products ALTER COLUMN ram_id DROP NOT NULL"))
    await db.execute(text("ALTER TABLE pm_products ALTER COLUMN storage_id DROP NOT NULL"))
    # v5 (2026-10-18): 12-digit SKU — column, unique index, backfill
    await db.execute(text("ALTER TABLE pm_products ADD COLUMN IF NOT EXISTS sku VARCHAR(12)"))
    await db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_pm_products_sku ON pm_products (sku)"))
    # v4 (2026-10-18): saved RAM / Storage picks; backfilled from linked rows
    for col in ("ram_type", "ram_size", "storage_type", "storage_size"):
        await db.execute(text(f"ALTER TABLE pm_products ADD COLUMN IF NOT EXISTS {col} VARCHAR(100)"))
    await db.execute(text(
        "UPDATE pm_products p SET ram_type = r.ram_category, ram_size = r.ram_capacity "
        "FROM hw_ram r WHERE p.ram_id = r.id AND p.ram_type IS NULL AND p.ram_size IS NULL"))
    await db.execute(text(
        "UPDATE pm_products p SET storage_type = s.storage_category, storage_size = s.storage_capacity "
        "FROM hw_storage s WHERE p.storage_id = s.id AND p.storage_type IS NULL AND p.storage_size IS NULL"))
    rows = (await db.execute(select(PMProduct))).scalars().all()
    used_skus = {p.sku for p in rows if p.sku}
    for p in rows:
        if not p.sku:
            p.sku = new_sku(used_skus)
    taken = {p.product_name.lower() for p in rows}
    for p in rows:
        new = build_product_name(p.make, p.model, p.cpu, p.cpu_gen)
        if new != p.product_name and new.lower() not in taken:
            taken.discard(p.product_name.lower())
            p.product_name = new
            taken.add(new.lower())
    await db.commit()


# ── Competitive Pricings (schema v3, dbdiagram approved 2026-10-14) ───────────

COMPETITIVE_METHOD_KEY = "pm_competitive_method"
COMPETITIVE_METHODS = [("average", "Average"), ("maximum", "Maximum"), ("minimum", "Minimum")]


class PMCompetitiveList(Base):
    __tablename__ = "pm_competitive_lists"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    display_order = Column(Integer, default=0)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)


class PMCompetitorPrice(Base):
    __tablename__ = "pm_competitor_prices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("pm_products.id"), nullable=False, index=True)
    list_id = Column(UUID(as_uuid=True), ForeignKey("pm_competitive_lists.id"), nullable=False, index=True)
    price = Column(Numeric(12, 2), nullable=False)
    updated_by = Column(String(50), nullable=True)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    __table_args__ = (
        UniqueConstraint("product_id", "list_id", name="uq_pm_competitor_product_list"),
    )


async def competitive_method(db) -> str:
    from sqlalchemy import select
    from models.settings import AppSetting
    v = (await db.execute(select(AppSetting.value).where(AppSetting.key == COMPETITIVE_METHOD_KEY))).scalar()
    return v if v in dict(COMPETITIVE_METHODS) else "average"


def apply_method(method: str, prices) -> "Decimal | None":
    vals = [Decimal(p) for p in prices if p is not None]
    if not vals:
        return None
    if method == "maximum":
        return _q(max(vals))
    if method == "minimum":
        return _q(min(vals))
    return _q(sum(vals) / len(vals))


async def recompute_sell_prices(db, product_ids=None) -> None:
    """Sell Price (pm_products.product_price) = the Formula Method over each
    product's filled competitor prices; none filled -> empty. Does not commit."""
    from sqlalchemy import select
    method = await competitive_method(db)
    q = select(PMProduct)
    if product_ids is not None:
        if not product_ids:
            return
        q = q.where(PMProduct.id.in_(list(product_ids)))
    else:
        # all products that have competitor prices — a Sell Price saved before
        # Competitive Pricings existed is left alone
        q = q.where(PMProduct.id.in_(select(PMCompetitorPrice.product_id)))
    products = (await db.execute(q)).scalars().all()
    if not products:
        return
    rows = (await db.execute(select(PMCompetitorPrice.product_id, PMCompetitorPrice.price)
                             .where(PMCompetitorPrice.product_id.in_([p.id for p in products])))).all()
    by_product = {}
    for pid, price in rows:
        by_product.setdefault(pid, []).append(price)
    for p in products:
        p.product_price = apply_method(method, by_product.get(p.id, []))


PM_SYNC_MARKER = "pm_sync_staging_20261014"


async def sync_pm_from_staging_once(db) -> str:
    """One-time sync (2026-10-14) of Dropdown Configuration + Competitive
    Pricings from staging (data/pm_sync_staging_20261014.json). Runs once,
    guarded by an app_settings marker row.

    Dropdowns are made to match staging: missing values added, a value whose
    staging spelling differs only by the old "<CPU>/" prefix is renamed, and
    values staging does not have are removed (audit-logged; products keep their
    saved text). Competitive lists missing on production are added (none are
    removed, so no entered prices are lost) and the Formula Method is set."""
    import json, os
    from sqlalchemy import select, delete as sa_delete
    from models.settings import AppSetting
    from services.audit_engine import audit
    if (await db.execute(select(AppSetting).where(AppSetting.key == PM_SYNC_MARKER))).scalar_one_or_none():
        return ""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                        "pm_sync_staging_20261014.json")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rows = (await db.execute(select(PMDropdownValue))).scalars().all()
    by_id = {r.id: r for r in rows}
    keep, added, renamed = set(), 0, 0
    staged_ids = {}            # (category, value) -> id, for linking children
    order = {}
    for it in data["dropdowns"]:                       # parents come before children
        cat, val, par = it["category"], it["value"], it.get("parent")
        parent_id = staged_ids.get((DROPDOWN_PARENT.get(cat), par)) if par else None
        if par and parent_id is None:
            continue
        def same_parent(r):
            return (r.parent_id == parent_id) if par else True
        match = next((r for r in rows if r.category == cat and r.id not in keep and same_parent(r)
                      and r.value.lower() in (val.lower(), f"{par}/{val}".lower() if par else val.lower())), None)
        if match is None:
            match = PMDropdownValue(id=uuid.uuid4(), category=cat, value=val, parent_id=parent_id,
                                    created_by="staging-sync")
            db.add(match)
            added += 1
        elif match.value != val:
            match.value = val
            renamed += 1
        match.display_order = order.get(cat, 0)
        order[cat] = order.get(cat, 0) + 1
        keep.add(match.id)
        staged_ids[(cat, val)] = match.id
    await db.flush()

    # remove values staging does not have — children before parents
    gone = [r for r in rows if r.id not in keep]
    gone.sort(key=lambda r: 0 if r.parent_id else 1)
    for r in gone:
        await audit(db, action="PM_DROPDOWN_DELETED", user=None, table_name="pm_dropdown_values",
                    record_id=str(r.id), old_value={"category": r.category, "value": r.value},
                    notes="Removed by one-time staging sync 2026-10-14")
        await db.execute(sa_delete(PMDropdownValue).where(PMDropdownValue.id == r.id))

    have = {n.lower() for n in (await db.execute(select(PMCompetitiveList.name))).scalars().all()}
    lists_added = 0
    for i, name in enumerate(data.get("competitive_lists", [])):
        if name.lower() not in have:
            db.add(PMCompetitiveList(name=name, display_order=i, created_by="staging-sync"))
            lists_added += 1
    method = data.get("method")
    if method in dict(COMPETITIVE_METHODS):
        row = (await db.execute(select(AppSetting).where(AppSetting.key == COMPETITIVE_METHOD_KEY))).scalar_one_or_none()
        if row:
            row.value = method
        else:
            db.add(AppSetting(key=COMPETITIVE_METHOD_KEY, value=method,
                              description="Product Master: Competitive Pricings formula method"))
    db.add(AppSetting(key=PM_SYNC_MARKER, value="done", description="One-time Product Master staging sync"))
    await db.flush()
    await recompute_sell_prices(db)
    await db.commit()
    return (f"{added} added, {renamed} renamed, {len(gone)} removed dropdown value(s); "
            f"{lists_added} competitive list(s) added; method={method}")


# ── Pricing Calculator uploads (schema v5, approved 2026-10-18) ───────────────
# Only what the file said is stored. Every price is worked out live from
# Product Master / Spare Parts / Hardware, so a Pending value fills in as soon
# as it is priced there.

class PMCalcUpload(Base):
    __tablename__ = "pm_calc_uploads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_name = Column(String(255), nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(String(50), nullable=True)
    uploaded_at = Column(DateTime, default=app_now, nullable=False)


class PMCalcRow(Base):
    __tablename__ = "pm_calc_rows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    upload_id = Column(UUID(as_uuid=True), ForeignKey("pm_calc_uploads.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    row_no = Column(Integer, nullable=False)
    sku = Column(String(40), nullable=True)
    make = Column(String(100), nullable=True)
    model = Column(String(100), nullable=True)
    cpu = Column(String(100), nullable=True)
    cpu_gen = Column(String(210), nullable=True)
    ram_type = Column(String(100), nullable=True)
    ram_size = Column(String(100), nullable=True)
    storage_type = Column(String(100), nullable=True)
    storage_size = Column(String(100), nullable=True)
    # comma-separated PART_FIELDS keys the file marked Damage / Not Working
    damaged_parts = Column(String(300), nullable=True)
