"""Repair cost per tag (2026-10-29, Pankaj) — one formula for Business P&L,
Lot P&L, Final QC and the Ready for Sale Min Selling Price.

    repair cost = labour + Packing Charges (if it applies) + Freight Charges (if it applies)

Labour (Additional Rates page, Labor Rates panel): the tag's device category
rate — "With L3/L4" when the tag has L3/L4 history, else "Without L3/L4". Only
tags that were worked on (went through L1/L2, L3/L4, Stress, any Cosmetic stage
or any Final QC stage) carry labour; a "With L3/L4" rate of 0 means 0 (not yet
filled in), and a category with no rate set costs 0.

Packing / Freight: a flat Rs per tag, each applied to "Tags" (not in an As-Is
lot), "As-Is Lot" tags, "Both", or neither — chosen on the same page. As-Is lot
tags never carry labour in the Ready for Sale Min Selling Price (they are sold
as-is, not repaired).

Everything is stored in cost_config (key / value), no schema change:
  packing_rate         Packing Charges (Rs) — its own setting, starts at 0 (the
                       old "Other Rates per Device", cosmetic_rate, is no longer used)
  packing_apply        0 none, 1 tags, 2 As-Is lot, 3 both
  freight_rate         Freight Charges (Rs)
  freight_apply        0 / 1 / 2 / 3, as above
  labour_wo_<cat>      Without L3/L4 (Rs) for a device category
  labour_w_<cat>       With L3/L4 (Rs)
Rates never saved fall back to LABOUR_DEFAULTS (the 2026-10-29 Labor Structure
sheet), so the pages work before anyone presses Save.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.cost_config import CostConfig

_ZERO = Decimal("0")

# (category, Without L3/L4, With L3/L4) — Labor Structure sheet, 2026-10-29.
LABOUR_DEFAULTS = [
    ("Laptop", 700, 1200), ("Desktop", 700, 1200), ("TFT Monitor", 700, 1200),
    ("Printer", 700, 0), ("Tablet", 700, 0), ("Mobile", 700, 0), ("Mini PC", 700, 0),
    ("Server", 1200, 0), ("Router", 1200, 0), ("Switch", 1200, 0), ("Workstation", 1200, 0),
]
# Device-type spellings seen on tags that mean one of the categories above.
_ALIASES = {"mobile phone": "mobile", "mobile phones": "mobile", "monitor": "tft monitor",
            "tft": "tft monitor", "laptops": "laptop", "desktops": "desktop"}

APPLY_NONE, APPLY_TAGS, APPLY_ASIS, APPLY_BOTH = 0, 1, 2, 3
APPLY_CHOICES = [(APPLY_TAGS, "On Tags (without As-Is Lot)"), (APPLY_ASIS, "On As-Is Lot"),
                 (APPLY_BOTH, "On Both"), (APPLY_NONE, "Not applied")]


def cat_slug(name) -> str:
    n = (name or "").strip().lower()
    n = _ALIASES.get(n, n)
    return re.sub(r"[^a-z0-9]+", "_", n).strip("_")


def device_category(device) -> str:
    """Device Type, else Sub Category — the same rule as the Ready for Sale tables."""
    return ((getattr(device, "device_type", None) or "").strip()
            or (getattr(device, "sub_category", None) or "").strip())


def is_as_is(device) -> bool:
    return bool((getattr(device, "sub_lot_number", None) or "").strip())


def _applies(code: int, as_is: bool) -> bool:
    return code == APPLY_BOTH or (code == APPLY_TAGS and not as_is) or (code == APPLY_ASIS and as_is)


@dataclass(frozen=True)
class RepairRates:
    labour: dict = field(default_factory=dict)        # slug -> (without, with)
    packing: Decimal = _ZERO
    packing_apply: int = APPLY_TAGS
    freight: Decimal = _ZERO
    freight_apply: int = APPLY_NONE

    def labour_for(self, device, had_l34: bool) -> Decimal:
        wo, w = self.labour.get(cat_slug(device_category(device)), (_ZERO, _ZERO))
        return w if had_l34 else wo

    def packing_for(self, device) -> Decimal:
        return self.packing if _applies(self.packing_apply, is_as_is(device)) else _ZERO

    def freight_for(self, device) -> Decimal:
        return self.freight if _applies(self.freight_apply, is_as_is(device)) else _ZERO

    def breakdown(self, device, worked: bool, had_l34: bool, labour_ok: bool = True) -> dict:
        lab = self.labour_for(device, had_l34) if (worked and labour_ok) else _ZERO
        pk, fr = self.packing_for(device), self.freight_for(device)
        return {"labour": lab, "packing": pk, "freight": fr, "total": lab + pk + fr}

    def cost(self, device, worked: bool, had_l34: bool, labour_ok: bool = True) -> Decimal:
        return self.breakdown(device, worked, had_l34, labour_ok)["total"]


def labour_categories(extra_names: Iterable[str] = ()) -> list:
    """[(label, slug, default_without, default_with)] for the Labor Rates panel:
    the sheet's categories, then any other Device Type (Master Data) not in it."""
    out, seen = [], set()
    for name, wo, w in LABOUR_DEFAULTS:
        out.append((name, cat_slug(name), wo, w))
        seen.add(cat_slug(name))
    for name in extra_names:
        s = cat_slug(name)
        if s and s not in seen:
            out.append((name, s, 0, 0))
            seen.add(s)
    return out


async def load_repair_rates(db: AsyncSession) -> RepairRates:
    rows = dict((await db.execute(select(CostConfig.key, CostConfig.value).where(or_(
        CostConfig.key.in_(["packing_rate", "packing_apply", "freight_rate", "freight_apply"]),
        CostConfig.key.like("labour_wo_%"), CostConfig.key.like("labour_w_%"),
    )))).all())
    labour = {cat_slug(n): (Decimal(wo), Decimal(w)) for n, wo, w in LABOUR_DEFAULTS}
    for k, v in rows.items():
        if v is None:
            continue
        if k.startswith("labour_wo_"):
            s = k[len("labour_wo_"):]
            labour[s] = (Decimal(v), labour.get(s, (_ZERO, _ZERO))[1])
        elif k.startswith("labour_w_"):
            s = k[len("labour_w_"):]
            labour[s] = (labour.get(s, (_ZERO, _ZERO))[0], Decimal(v))
    return RepairRates(
        labour=labour,
        packing=Decimal(rows.get("packing_rate") or 0),
        packing_apply=int(rows["packing_apply"]) if rows.get("packing_apply") is not None else APPLY_TAGS,
        freight=Decimal(rows.get("freight_rate") or 0),
        freight_apply=int(rows["freight_apply"]) if rows.get("freight_apply") is not None else APPLY_NONE,
    )


async def worked_and_l34_ids(db: AsyncSession, device_ids=None) -> tuple[set, set]:
    """(worked, had_l34) device ids (as str). Worked = moved into a labour stage
    (services/business_pl.LABOUR_STAGES). L3/L4 history = moved to L3/L4 or has
    an L3/L4 WorkID. device_ids None = every device."""
    from models.device import DeviceStage, StageMovement
    from models.work_order import WorkOrder
    from services.business_pl import LABOUR_STAGES
    ids = None if device_ids is None else [d for d in device_ids if d]
    if ids is not None and not ids:
        return set(), set()
    worked, l34 = set(), set()

    def chunks():
        if ids is None:
            yield None
        else:
            for i in range(0, len(ids), 5000):
                yield ids[i:i + 5000]

    for part in chunks():
        mw = [StageMovement.to_stage.in_(LABOUR_STAGES)]
        lw = [StageMovement.to_stage == DeviceStage.l3]
        ww = [or_(WorkOrder.work_id.like("L3L4-%"), WorkOrder.stage == "l3")]
        if part is not None:
            mw.append(StageMovement.device_id.in_(part))
            lw.append(StageMovement.device_id.in_(part))
            ww.append(WorkOrder.device_id.in_(part))
        worked.update(str(d) for d in (await db.execute(
            select(StageMovement.device_id).where(*mw).distinct())).scalars().all())
        l34.update(str(d) for d in (await db.execute(
            select(StageMovement.device_id).where(*lw).distinct())).scalars().all())
        l34.update(str(d) for d in (await db.execute(
            select(WorkOrder.device_id).where(*ww).distinct())).scalars().all() if d)
    return worked, l34


async def lot_repair_costs(db: AsyncSession, device_where=(), lot_ids=None) -> dict:
    """{lot_id: (labour, packing_freight, sold_labour)} for Lot P&L (Dashboard,
    Lot detail, Lot P&L report). Labour: every worked-on tag in the lot (the work is done whether
    or not it has sold). Packing + Freight: the lot's SOLD tags only — they are
    dispatch costs, as in Business P&L. `device_where` narrows the devices
    (e.g. the P&L date filter's completed devices)."""
    from models.device import Device
    from models.sales import Sale
    w = [Device.is_trashed.isnot(True), *device_where]
    if lot_ids is not None:
        w.append(Device.lot_id.in_(list(lot_ids)))
    devs = (await db.execute(
        select(Device.id, Device.lot_id, Device.device_type, Device.sub_category, Device.sub_lot_number)
        .where(*w)
    )).all()
    if not devs:
        return {}
    rates = await load_repair_rates(db)
    ids = [d.id for d in devs]
    one_lot = lot_ids is not None and len(list(lot_ids)) <= 5
    worked, l34 = await worked_and_l34_ids(db, ids if one_lot else None)
    sold = set()
    for i in range(0, len(ids), 5000) if one_lot else [None]:
        q = select(Sale.device_id).distinct()
        if i is not None:
            q = q.where(Sale.device_id.in_(ids[i:i + 5000]))
        sold.update(str(x) for x in (await db.execute(q)).scalars().all())
    out: dict = {}
    for d in devs:
        sid = str(d.id)
        lab = rates.labour_for(d, sid in l34) if sid in worked else _ZERO
        pf = (rates.packing_for(d) + rates.freight_for(d)) if sid in sold else _ZERO
        cur = out.get(d.lot_id, (_ZERO, _ZERO, _ZERO))
        # (labour, packing + freight, labour of the sold tags only)
        out[d.lot_id] = (cur[0] + lab, cur[1] + pf, cur[2] + (lab if sid in sold else _ZERO))
    return out
