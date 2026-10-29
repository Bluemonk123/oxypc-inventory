"""Ready to Sale Min Selling Price formulas — computed server-side only.

Tag table:      unit price + repair cost + margin for each item
As-Is Lot:      lot price  + Packing/Freight of its tags (if ticked "On As-Is
                Lot") + margin for As-Is lot
Repair cost (2026-10-29): services/repair_cost.py — Labor Rate by device type
(With L3/L4 when the tag had L3/L4; worked-on tags) + Packing + Freight. It
replaced the flat "Other Rates per Device".
Lot price:      sum of the available tags' unit prices (= availability x device
                price when every tag carries the same price)

A margin is either an Amount (used as-is) or a Percentage (applied to the base
price it is added to) — never both. Cost Config stores each as two keys
(<name>_amount / <name>_percent) and the save handler zeroes the unused one;
if both somehow hold a value, the amount wins.

"Set price" overrides (Device.min_selling_price / AsIsLotPrice) are applied by
the caller: an explicit stored value always beats the formula.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.cost_config import CostConfig

_ZERO = Decimal("0")
_PAISE = Decimal("0.01")

# "cosmetic_rate" (Other Rates per Device) is no longer read (2026-10-29):
# Packing / Freight / labour come from services/repair_cost.py.
RATE_KEYS = (
    "margin_item_amount", "margin_item_percent",
    "margin_asis_amount", "margin_asis_percent",
)


@dataclass(frozen=True)
class ReadyRates:
    other_rate: Decimal = _ZERO
    margin_item_amount: Decimal = _ZERO
    margin_item_percent: Decimal = _ZERO
    margin_asis_amount: Decimal = _ZERO
    margin_asis_percent: Decimal = _ZERO


async def load_ready_rates(db: AsyncSession) -> ReadyRates:
    rows = (await db.execute(
        select(CostConfig.key, CostConfig.value).where(CostConfig.key.in_(RATE_KEYS))
    )).all()
    vals = {k: Decimal(v) for k, v in rows if v is not None}
    return ReadyRates(
        other_rate=_ZERO,
        margin_item_amount=vals.get("margin_item_amount", _ZERO),
        margin_item_percent=vals.get("margin_item_percent", _ZERO),
        margin_asis_amount=vals.get("margin_asis_amount", _ZERO),
        margin_asis_percent=vals.get("margin_asis_percent", _ZERO),
    )


def unit_price(device, lot_buying_price, lot_qty) -> Optional[Decimal]:
    """Per-tag price — the same value the Unit Cost column shows.

    The device's own price when set, otherwise the lot's average (buying price
    / qty). None when neither exists, so callers can show "—" rather than a
    misleading zero.
    """
    if device.device_price:
        return Decimal(device.device_price)
    if lot_qty:
        return Decimal(lot_buying_price or 0) / Decimal(lot_qty)
    return None


def margin_value(base: Decimal, amount: Decimal, percent: Decimal) -> Decimal:
    if amount and amount > 0:
        return Decimal(amount)
    if percent and percent > 0:
        return Decimal(base) * Decimal(percent) / Decimal(100)
    return _ZERO


def _money(v: Decimal) -> Decimal:
    return v.quantize(_PAISE, rounding=ROUND_HALF_UP)


def tag_min_price(unit: Optional[Decimal], rates: ReadyRates,
                  repair: Optional[Decimal] = None) -> Optional[Decimal]:
    """repair = the tag's repair cost (services/repair_cost.py); callers pass
    it. Without it, the old flat Other Rates is used (kept for safety)."""
    if unit is None:
        return None
    extra = Decimal(repair) if repair is not None else rates.other_rate
    return _money(unit + extra
                  + margin_value(unit, rates.margin_item_amount, rates.margin_item_percent))


def lot_price(unit_prices: Iterable[Optional[Decimal]]) -> Decimal:
    return _money(sum((u for u in unit_prices if u is not None), _ZERO))


def as_is_min_price(lot_total: Decimal, rates: ReadyRates, extra: Decimal = _ZERO) -> Decimal:
    """extra = Packing + Freight of the lot's tags when ticked "On As-Is Lot"
    (2026-10-29). The margin stays on the lot price."""
    return _money(lot_total + Decimal(extra or 0)
                  + margin_value(lot_total, rates.margin_asis_amount, rates.margin_asis_percent))
