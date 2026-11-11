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


def verify_eligible(device, in_batch: dict) -> bool:
    """Tags Scrapped Verify (single or bulk) may act on this tag: on Tags
    Scrapped (Scrap for Sale stage, not trashed, not in a live batch) and
    either verifiable now, or verified earlier but never batched (tags
    verified before Verify started making Scrap IDs, 2026-11-09)."""
    if device is None or device.is_trashed or device.current_stage != DeviceStage.scrap_for_sale:
        return False
    if device.id in in_batch:
        return False
    return bool(device.scrap_verified) or can_verify(device)


async def verify_into_scrap_batch(db: AsyncSession, devices, current_user, notes: str = ""):
    """Tags Scrapped Verify (2026-11-09, Pankaj): mark the tags verified and put
    them into ONE new Scrap ID on Scrap for Sale — the same ScrapForSale +
    StockTransfer(scrap_for_sale) rows a Transfers "Scrap for Sale"
    submission makes, so Scrap for Sale lists, prices and sells it the same
    way. No Assigned To and no WorkIDs (Pankaj's choice); no location move.
    Returns the new ScrapForSale (None when devices is empty). The caller
    commits."""
    from models.stock_transfer import StockTransfer
    from models.lot import Lot
    from routers.transfers import _next_scrap_id
    from utils.timezone import app_now
    devices = list(devices)
    if not devices:
        return None
    batch = ScrapForSale(scrap_id=await _next_scrap_id(db), assigned_to_user_id=None,
                         created_by=current_user.username)
    db.add(batch)
    await db.flush()
    lot_ids = {d.lot_id for d in devices if getattr(d, "lot_id", None)}
    lot_map = {}
    if lot_ids:
        lot_map = dict((await db.execute(select(Lot.id, Lot.lot_number).where(Lot.id.in_(lot_ids)))).all())
    now = app_now()
    note = (notes or "").strip()
    for d in devices:
        d.scrap_verified = True
        if note:
            d.notes = ((d.notes or "") + "\n[Verify] " + note).strip()
        d.updated_at = now
        wh = getattr(d, "warehouse", None) or "—"
        db.add(StockTransfer(
            device_id=d.id, move_kind="device", transfer_type="scrap_for_sale",
            scrap_for_sale_id=batch.id, source="tags_scrapped_verify",
            from_warehouse=wh, to_warehouse=wh,
            transferred_by=current_user.username,
            barcode=d.barcode, serial_no=d.serial_no, make=d.brand, model=d.model,
            cpu=getattr(d, "cpu", None), generation=getattr(d, "generation", None),
            ram=f"{d.ram_gb} GB" if d.ram_gb else None,
            hdd=f"{d.storage_gb} GB" if d.storage_gb else None,
            category=d.sub_category, lot_number=lot_map.get(d.lot_id),
            product_stage=d.current_stage.value if d.current_stage else None,
            transfer_date=now, notes=f"Verified on Tags Scrapped{(' — ' + note) if note else ''}",
            created_by=current_user.username,
        ))
    await db.flush()
    return batch


def refused_text(refused) -> str:
    """'A (not verified), B (already in Scrap ID 0003)' — first 10, then a count."""
    parts = [f"{bc} ({why})" for bc, why in refused[:10]]
    if len(refused) > 10:
        parts.append(f"+{len(refused) - 10} more")
    return ", ".join(parts)
