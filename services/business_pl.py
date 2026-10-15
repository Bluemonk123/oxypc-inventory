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

    # ── Labour Cost (2026-10-15): a flat per-tag labour charge — Cost Config's
    # "Labour Rate per Repair Attempt" (repair_labour_rate) — for every sold
    # tag that passed through L1/L2, L3/L4, Stress Test, any Cosmetic stage or
    # any Final QC stage (a StageMovement into one of them). Tags that never
    # entered any of those stages carry no labour cost.
    rate_row = (await db.execute(
        select(CostConfig.value).where(CostConfig.key == "repair_labour_rate")
    )).scalar_one_or_none()
    labour_rate = float(rate_row) if rate_row is not None else 150.0
    worked_on = (
        select(StageMovement.device_id)
        .where(StageMovement.to_stage.in_(LABOUR_STAGES))
        .distinct()
    )
    labour_result = await db.execute(
        select(
            extract("month", Sale.sold_at).label("month"),
            func.count(func.distinct(Sale.device_id)).label("tags"),
        )
        .where(Sale.device_id.in_(worked_on))
        .where(extract("year", Sale.sold_at) == year)
        .group_by(extract("month", Sale.sold_at))
    )
    labour_by_month = {int(r.month): int(r.tags) * labour_rate for r in labour_result}
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
