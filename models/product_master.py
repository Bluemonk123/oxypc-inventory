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
    ("ram_category", "RAM Category"),
    ("ram_capacity", "RAM Capacity"),
    ("storage_category", "Storage Category"),
    ("storage_capacity", "Storage Capacity"),
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
    ram_id = Column(UUID(as_uuid=True), ForeignKey("hw_ram.id"), nullable=False)
    storage_id = Column(UUID(as_uuid=True), ForeignKey("hw_storage.id"), nullable=False)
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


def build_product_name(make, model, cpu_gen, ram_name, storage_name) -> str:
    return "/".join([make, model, cpu_gen, ram_name, storage_name])


def parts_done(p: PMProduct) -> bool:
    return all(getattr(p, col) is not None for col, _ in PART_FIELDS)


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def product_figures(p: PMProduct, ram_price, storage_price) -> dict:
    """Server-side pricing for one product (Decimal throughout).
    Base Price    = Product Price - RAM Price - Storage Price
    Discount Amt  = Product Price x Discount% / 100 (0 while Product Price is pending)
    Selling Price = (Base Price - Discount Amt) + (RAM Price + Storage Price)
    Base / Selling are None while Product Price is pending."""
    ram = Decimal(ram_price or 0)
    sto = Decimal(storage_price or 0)
    pct = Decimal(p.discount_pct or 0)
    if p.product_price is None:
        return {"base_price": None, "discount_amount": Decimal("0.00"), "selling_price": None}
    price = Decimal(p.product_price)
    base = price - ram - sto
    disc = _q(price * pct / Decimal(100))
    return {"base_price": _q(base), "discount_amount": disc,
            "selling_price": _q((base - disc) + (ram + sto))}
