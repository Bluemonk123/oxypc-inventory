"""
Cost Engine
-----------
Maintains per-device costing and per-lot P&L.

Key rules:
  - base_cost  = lot.buying_price / lot.qty (at device creation)
  - parts_cost = SUM(spare_parts_consumption.total_cost WHERE device_id)
  - total_cost = base_cost + parts_cost + labour_cost
  - Scrap trigger: total_cost > expected_sale_value
  - Below-cost warning: sale_price < total_cost  (warns, does not block)
"""
from __future__ import annotations
import json
from decimal import Decimal
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from models.engines import DeviceCosting, AuditLog, RepairAttempt
from models.device import Device, DeviceStage, StageMovement
from models.lot import Lot
from models.spare_parts import SparePartConsumption


SCRAP_WARNING_RATIO = Decimal("0.70")   # warn if cost > 70% of expected sale value
WARNING_RATIO       = Decimal("0.90")   # below-cost warning threshold


def _base_cost_for(device: Device, lot: Lot | None) -> Decimal:
    """Landed per-unit cost: (lot acquisition price + GST) / qty, unless the device
    carries its own price. Shared by the single and batch costing paths so the two
    can never drift apart on the money maths."""
    base = Decimal("0")
    if lot and lot.buying_price and lot.qty:
        gst_total = (
            Decimal(str(lot.sgst or 0))
            + Decimal(str(lot.cgst or 0))
            + Decimal(str(lot.igst or 0))
        )
        base = (Decimal(str(lot.buying_price)) + gst_total) / Decimal(str(max(lot.qty, 1)))
    if device.device_price:
        base = Decimal(str(device.device_price))
    return base


async def get_or_create_costings(
    devices: list[Device], db: AsyncSession
) -> dict:
    """Batch form of get_or_create_costing: {device.id: DeviceCosting} in 2 queries.

    The per-device version costs 1-2 round trips each, which is fine for one device
    and ruinous for a bulk sale — 600 devices meant 1,200 sequential round trips to
    a database on another continent. This resolves the whole set at once.

    Rows created here are only added to the session; the caller commits.
    """
    if not devices:
        return {}

    # Chunked (2026-10-19): one IN() per 5,000 ids keeps every query far
    # below Postgres/asyncpg's 32,767 bind-parameter cap, so a bulk sale of
    # any size (10,000+ tags) works.
    device_ids = [d.id for d in devices]
    by_device = {}
    for i in range(0, len(device_ids), 5000):
        for c in (await db.execute(
            select(DeviceCosting).where(DeviceCosting.device_id.in_(device_ids[i:i + 5000]))
        )).scalars().all():
            by_device[c.device_id] = c

    missing = [d for d in devices if d.id not in by_device]
    if missing:
        lot_ids = {d.lot_id for d in missing if d.lot_id}
        lots = {}
        if lot_ids:
            lots = {l.id: l for l in (await db.execute(
                select(Lot).where(Lot.id.in_(lot_ids))
            )).scalars().all()}
        for d in missing:
            base = _base_cost_for(d, lots.get(d.lot_id))
            costing = DeviceCosting(
                device_id=d.id, base_cost=base,
                parts_cost=Decimal("0"), labour_cost=Decimal("0"),
                total_cost=base, expected_sale_value=None,
            )
            db.add(costing)
            by_device[d.id] = costing
    return by_device


def below_cost_warning_for(costing: DeviceCosting, sale_price: Decimal) -> str | None:
    """Pure form of check_below_cost_warning for an already-loaded costing row."""
    total = (costing.total_cost if costing else None) or Decimal("0")
    if total > 0 and Decimal(str(sale_price)) < total:
        return (
            f"BELOW-COST WARNING: sale price ₹{float(sale_price):.2f} is less than "
            f"device total cost ₹{float(total):.2f}. Sale will proceed but profit is negative."
        )
    return None


async def get_or_create_costing(device: Device, db: AsyncSession) -> DeviceCosting:
    """Fetch (or create) the DeviceCosting row for this device."""
    result = await db.execute(
        select(DeviceCosting).where(DeviceCosting.device_id == device.id)
    )
    costing = result.scalar_one_or_none()
    if not costing:
        # Calculate base cost from lot
        lot_result = await db.execute(select(Lot).where(Lot.id == device.lot_id))
        lot = lot_result.scalar_one_or_none()
        expected = None
        base = _base_cost_for(device, lot)

        costing = DeviceCosting(
            device_id=device.id,
            base_cost=base,
            parts_cost=Decimal("0"),
            labour_cost=Decimal("0"),
            total_cost=base,
            expected_sale_value=expected,
        )
        db.add(costing)
    return costing


async def refresh_parts_cost(device: Device, db: AsyncSession) -> DeviceCosting:
    """Recalculate parts_cost and labour_cost; update total_cost.

    parts_cost  = SUM(spare_parts_consumption.total_cost WHERE device_id)
    labour_cost = SUM(repair_attempts.cost WHERE device_id)
    total_cost  = base_cost + parts_cost + labour_cost
    """
    costing = await get_or_create_costing(device, db)

    parts_sum_result = await db.execute(
        select(func.sum(SparePartConsumption.total_cost))
        .where(SparePartConsumption.device_id == device.id)
    )
    parts_sum = Decimal(str(parts_sum_result.scalar() or 0))

    labour_sum_result = await db.execute(
        select(func.sum(RepairAttempt.cost))
        .where(RepairAttempt.device_id == device.id)
    )
    labour_sum = Decimal(str(labour_sum_result.scalar() or 0))

    costing.parts_cost  = parts_sum
    costing.labour_cost = labour_sum
    costing.total_cost  = costing.base_cost + costing.parts_cost + costing.labour_cost
    costing.updated_at  = app_now()
    return costing


async def check_scrap_decision(
    device: Device,
    db: AsyncSession,
    current_user_name: str = "system",
) -> dict:
    """
    After a repair attempt, decide whether to auto-scrap.
    Returns:
      { "scrap": bool, "warning": bool, "total_cost": float, "expected_sale": float, "reason": str }
    """
    costing = await refresh_parts_cost(device, db)

    total = costing.total_cost or Decimal("0")
    expected = costing.expected_sale_value

    result = {"scrap": False, "warning": False,
              "total_cost": float(total), "expected_sale": float(expected or 0),
              "reason": ""}

    if expected and expected > 0:
        if total >= expected:
            result["scrap"] = True
            result["reason"] = (
                f"Repair cost ₹{total:.2f} >= expected sale value ₹{expected:.2f}. "
                "Auto-scrapping device."
            )
        elif total >= expected * SCRAP_WARNING_RATIO:
            result["warning"] = True
            result["reason"] = (
                f"Repair cost ₹{total:.2f} is {float(total/expected*100):.0f}% of "
                f"expected sale ₹{expected:.2f}. Review profitability."
            )

    return result


async def auto_scrap_device(
    device: Device,
    reason: str,
    db: AsyncSession,
    current_user_name: str = "system",
) -> None:
    """Move device to scrapped stage with audit log. Called by cost engine."""
    prev = device.current_stage
    device.current_stage = DeviceStage.scrapped
    device.updated_at = app_now()
    movement = StageMovement(
        device_id=device.id,
        from_stage=prev,
        to_stage=DeviceStage.scrapped,
        moved_by=current_user_name,
        notes=f"AUTO-SCRAP (Cost Engine): {reason}",
    )
    db.add(movement)

    log = AuditLog(
        username=current_user_name,
        action="AUTO_SCRAP",
        table_name="devices",
        record_id=str(device.id),
        new_value=json.dumps({"barcode": device.barcode, "reason": reason}),
        notes=reason,
    )
    db.add(log)


async def check_below_cost_warning(
    device: Device,
    sale_price: Decimal,
    db: AsyncSession,
) -> str | None:
    """Return a warning message if sale_price < total_cost, else None."""
    costing = await get_or_create_costing(device, db)
    total = costing.total_cost or Decimal("0")
    if total > 0 and Decimal(str(sale_price)) < total:
        return (
            f"BELOW-COST WARNING: sale price ₹{float(sale_price):.2f} is less than "
            f"device total cost ₹{float(total):.2f}. Sale will proceed but profit is negative."
        )
    return None
