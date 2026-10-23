"""Scrap flow rules shared by Tags Scrapped, Scrap for Sale and Transfers
(2026-10-23).

    L3/L4 scrap -> L1/L2 Back to Inventory -> Tags Scrapped (Verify / Replace)
    -> Scrap for Sale (a Scrap ID batch, made through Transfers)

A tag is on Tags Scrapped until it is in a live Scrap ID batch; from then on
it is only inside that batch on Scrap for Sale. A Scrap for Sale transfer
takes only tags that Tags Scrapped is ready to hand over: Scrap for Sale
stage, Verified, not already in a live batch.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.device import DeviceStage
from models.scrap_for_sale import ScrapForSale
from models.stock_transfer import StockTransfer


def can_verify(device) -> bool:
    """Tags Scrapped offers Verify: not yet verified and, for a Replacement
    Scrap tag, only once its replacement tag has been set."""
    if device.scrap_verified:
        return False
    if (device.l34_status or "") == "Replacement Scrap":
        return bool(device.replace_with_barcode)
    return True


async def batched_device_ids(db: AsyncSession, device_ids=None) -> dict:
    """{device_id: scrap_id} for tags in a live (not removed) Scrap ID batch."""
    q = (select(StockTransfer.device_id, ScrapForSale.scrap_id)
         .join(ScrapForSale, StockTransfer.scrap_for_sale_id == ScrapForSale.id)
         .where(StockTransfer.device_id.isnot(None), ScrapForSale.is_trashed.isnot(True)))
    if device_ids is not None:
        ids = list(device_ids)
        if not ids:
            return {}
        q = q.where(StockTransfer.device_id.in_(ids))
    return {did: sid for did, sid in (await db.execute(q)).all()}


async def split_scrap_transfer(db: AsyncSession, devices) -> tuple[list, list]:
    """For a Scrap for Sale transfer: (eligible devices, [(barcode, reason)])."""
    in_batch = await batched_device_ids(db, [d.id for d in devices])
    ok, refused = [], []
    for d in devices:
        if d.is_trashed or d.current_stage != DeviceStage.scrap_for_sale:
            refused.append((d.barcode, "not on Tags Scrapped"))
        elif not d.scrap_verified:
            refused.append((d.barcode, "not verified"))
        elif d.id in in_batch:
            refused.append((d.barcode, f"already in Scrap ID {in_batch[d.id]}"))
        else:
            ok.append(d)
    return ok, refused


def refused_text(refused) -> str:
    """'A (not verified), B (already in Scrap ID 0003)' — first 10, then a count."""
    parts = [f"{bc} ({why})" for bc, why in refused[:10]]
    if len(refused) > 10:
        parts.append(f"+{len(refused) - 10} more")
    return ", ".join(parts)
