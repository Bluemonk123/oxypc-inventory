"""Shared Parts/Labour COGS-per-year computation — the single source both
Business P&L's Monthly Breakdown table (routers/reports.py business_pl) and
the Dashboard's Financial Summary card (routers/dashboard.py) call, so the
two never disagree on the same year's Parts Cost / Labour Cost figures.

Mirrors business_pl()'s own query shape and override-merge logic exactly;
extracted here so a future change to either only has to happen once.
"""
from sqlalchemy import select, func, extract
from sqlalchemy.ext.asyncio import AsyncSession

from models.device import DeviceStage, StageMovement, COSMETIC_STAGES
from models.sales import Sale
from models.spare_parts import SparePart
from models.part_request import PartRequest
from models.cost_config import CostConfig
from models.business_pl_override import BusinessPLOverride

# Stages whose work earns a tag the per-tag labour charge: L1/L2, L3/L4,
# Stress Test (qc_check), every Cosmetic stage and every Final QC stage.
LABOUR_STAGES = [DeviceStage.l1, DeviceStage.l2, DeviceStage.l3, DeviceStage.qc_check,
                 *COSMETIC_STAGES,
                 DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold]


async def _repair_cost_by(db: AsyncSession, keyed_devices) -> dict:
    """{key: repair cost} summed over (key, Device) pairs, each tag counted
    once per key (services/repair_cost.py)."""
    from services.repair_cost import load_repair_rates, worked_and_l34_ids
    rates = await load_repair_rates(db)
    seen, pairs = set(), []
    for k, d in keyed_devices:
        if (k, d.id) not in seen:
            seen.add((k, d.id))
            pairs.append((k, d))
    worked, l34 = await worked_and_l34_ids(db, list({d.id for _k, d in pairs}))
    out: dict = {}
    for k, d in pairs:
        out[k] = out.get(k, 0.0) + float(rates.cost(d, str(d.id) in worked, str(d.id) in l34))
    return out


async def compute_year_parts_labour_cogs(db: AsyncSession, year: int) -> tuple[list, list]:
    """Returns (monthly_parts_cogs, monthly_labour_cogs) — 12-element lists,
    Jan..Dec, for `year`, with any admin overrides (/reports/business-pl/override)
    already applied. Callers needing the yearly total just sum() the list."""
    # ── Parts Cost (2026-10-15): the parts actually changed on each sold tag —
    # the same source as Parts Manager's "Parts Consumption" tab: PartRequest
    # rows the engineer verified as Part Changed (status "received"), at
    # qty handed over × the part's unit price. (Was SparePartConsumption, a
    # table the current repair flow no longer writes to, so it showed ~0.)
    parts_result = await db.execute(
        select(
            extract("month", Sale.sold_at).label("month"),
            func.coalesce(func.sum(
                PartRequest.qty_handed_over * func.coalesce(SparePart.unit_price, 0)), 0).label("parts_cost"),
        )
        .join(Sale, Sale.device_id == PartRequest.device_id)
        .outerjoin(SparePart, PartRequest.part_id == SparePart.id)
        .where(PartRequest.status == "received")
        .where(extract("year", Sale.sold_at) == year)
        .group_by(extract("month", Sale.sold_at))
    )
    parts_by_month = {int(r.month): float(r.parts_cost) for r in parts_result}
    monthly_parts_cogs = [parts_by_month.get(m, 0.0) for m in range(1, 13)]

    # ── Labour Cost = repair cost (2026-10-29, services/repair_cost.py): for
    # every tag sold that month, its device category's labour (With L3/L4 rate
    # when it has L3/L4 history; only tags worked on in L1/L2, L3/L4, Stress,
    # Cosmetic or Final QC) + Packing + Freight Charges where they apply. Was a
    # flat "Labour Rate per Repair Attempt" per worked-on sold tag.
    from models.device import Device
    sold = (await db.execute(
        select(extract("month", Sale.sold_at).label("month"), Device)
        .join(Device, Sale.device_id == Device.id)
        .where(extract("year", Sale.sold_at) == year)
    )).all()
    labour_by_month = await _repair_cost_by(db, [(int(m), d) for m, d in sold])
    monthly_labour_cogs = [labour_by_month.get(m, 0.0) for m in range(1, 13)]

    overrides = {
        o.month: o for o in (await db.execute(
            select(BusinessPLOverride).where(BusinessPLOverride.year == year)
        )).scalars().all()
    }
    for idx in range(12):
        o = overrides.get(idx + 1)
        if not o:
            continue
        if o.parts_cogs_override is not None:
            monthly_parts_cogs[idx] = float(o.parts_cogs_override)
        if o.labour_cogs_override is not None:
            monthly_labour_cogs[idx] = float(o.labour_cogs_override)

    return monthly_parts_cogs, monthly_labour_cogs


async def compute_year_device_cogs(db: AsyncSession, year: int) -> list:
    """Business P&L's Device Cost column for `year` — 12 monthly sums of the
    Device Price of each tag sold that month, with any admin Device COGS
    override applied. Shared by the Dashboard's Financial Summary "Total
    Investment" (2026-10-15) so the two pages never disagree."""
    from models.device import Device
    rows = await db.execute(
        select(extract("month", Sale.sold_at).label("month"),
               func.coalesce(func.sum(Device.device_price), 0).label("device_cogs"))
        .join(Device, Sale.device_id == Device.id)
        .where(extract("year", Sale.sold_at) == year)
        .group_by(extract("month", Sale.sold_at))
    )
    by_month = {int(r.month): float(r.device_cogs) for r in rows}
    monthly = [by_month.get(m, 0.0) for m in range(1, 13)]
    for o in (await db.execute(
        select(BusinessPLOverride).where(BusinessPLOverride.year == year)
    )).scalars().all():
        if o.device_cogs_override is not None and 1 <= o.month <= 12:
            monthly[o.month - 1] = float(o.device_cogs_override)
    return monthly


async def compute_year_revenue(db: AsyncSession, year: int) -> list:
    """Business P&L's Revenue column for calendar `year` — 12 monthly sums of
    sale price, with any admin Revenue override applied."""
    rows = await db.execute(
        select(extract("month", Sale.sold_at).label("month"),
               func.coalesce(func.sum(Sale.sale_price), 0).label("revenue"))
        .where(extract("year", Sale.sold_at) == year)
        .group_by(extract("month", Sale.sold_at))
    )
    by_month = {int(r.month): float(r.revenue) for r in rows}
    monthly = [by_month.get(m, 0.0) for m in range(1, 13)]
    for o in (await db.execute(
        select(BusinessPLOverride).where(BusinessPLOverride.year == year)
    )).scalars().all():
        if o.revenue_override is not None and 1 <= o.month <= 12:
            monthly[o.month - 1] = float(o.revenue_override)
    return monthly


async def compute_fy_revenue(db: AsyncSession, fy_start: int) -> float:
    """Business P&L's Revenue total for financial year Apr fy_start .. Mar fy_start+1."""
    r1 = await compute_year_revenue(db, fy_start)
    r2 = await compute_year_revenue(db, fy_start + 1)
    return sum(r1[3:]) + sum(r2[:3])


async def compute_fy_totals(db: AsyncSession, fy_start: int) -> tuple[float, float, float]:
    """(device_cost, parts_cost, labour_cost) for the Indian financial year
    Apr fy_start .. Mar fy_start+1 (e.g. 2025 -> "2025-26") — Business P&L's
    Device / Parts / Labour Cost columns (admin overrides included) for
    Apr–Dec of fy_start plus Jan–Mar of fy_start+1. Used by the Dashboard's
    Financial Summary card (2026-10-15)."""
    d1 = await compute_year_device_cogs(db, fy_start)
    d2 = await compute_year_device_cogs(db, fy_start + 1)
    p1, l1 = await compute_year_parts_labour_cogs(db, fy_start)
    p2, l2 = await compute_year_parts_labour_cogs(db, fy_start + 1)
    return (sum(d1[3:]) + sum(d2[:3]), sum(p1[3:]) + sum(p2[:3]), sum(l1[3:]) + sum(l2[:3]))


def fy_label(fy_start: int) -> str:
    """2025 -> "2025-26"."""
    return f"{fy_start}-{str(fy_start + 1)[-2:]}"


async def compute_month_profit(db: AsyncSession, year: int, month: int) -> float:
    """Business P&L's Gross Profit for one calendar month: Revenue − Device
    Cost − Parts Cost − Labour Cost (admin overrides applied). Used by the
    Dashboard's "Profit This Month" card (2026-10-15)."""
    i = month - 1
    rev = (await compute_year_revenue(db, year))[i]
    dev = (await compute_year_device_cogs(db, year))[i]
    parts, labour = await compute_year_parts_labour_cogs(db, year)
    return rev - dev - parts[i] - labour[i]


async def compute_range_revenue_profit(db: AsyncSession, start=None, end=None, device_where=()) -> tuple[float, float]:
    """(revenue, gross profit) for sales dated start..end (datetimes, either
    may be None) — the Dashboard's Revenue / Profit tiles when a From/To date
    range is set (2026-10-22). Same definitions as Business P&L: Revenue −
    Device Cost − Parts Cost − Labour Cost, per sale date. Business P&L's
    monthly admin overrides can't be split across an arbitrary range, so
    they are not applied here. `device_where` adds Device filters (entity,
    device type, zone)."""
    from models.device import Device

    def in_range(col):
        w = []
        if start is not None:
            w.append(col >= start)
        if end is not None:
            w.append(col <= end)
        return w

    sales_where = [*in_range(Sale.sold_at), *device_where]
    rev, dev = (await db.execute(
        select(func.coalesce(func.sum(Sale.sale_price), 0), func.coalesce(func.sum(Device.device_price), 0))
        .select_from(Sale).join(Device, Sale.device_id == Device.id).where(*sales_where)
    )).one()
    parts = (await db.execute(
        select(func.coalesce(func.sum(PartRequest.qty_handed_over * func.coalesce(SparePart.unit_price, 0)), 0))
        .select_from(PartRequest)
        .join(Sale, Sale.device_id == PartRequest.device_id)
        .join(Device, Sale.device_id == Device.id)
        .outerjoin(SparePart, PartRequest.part_id == SparePart.id)
        .where(PartRequest.status == "received", *sales_where)
    )).scalar() or 0
    # Labour = repair cost per sold tag (2026-10-29), same as Business P&L.
    sold = (await db.execute(
        select(Device).select_from(Sale).join(Device, Sale.device_id == Device.id).where(*sales_where)
    )).scalars().all()
    labour = sum((await _repair_cost_by(db, [(0, d) for d in sold])).values())
    revenue = float(rev or 0)
    return revenue, revenue - float(dev or 0) - float(parts) - labour
