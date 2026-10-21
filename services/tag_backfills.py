"""One-time tag data backfills (2026-10-21, Pankaj).

Both run once at startup, guarded by an app_settings marker row (same pattern
as models.grn_import.backfill_grn_entity_once), and leave a summary row in the
audit log so the result can be read from /admin/audit-log.

1. backfill_tag_location_text_once — Zone / Location Type text on each tag
   (devices.floor / devices.warehouse) from its Location ID. Most pages already
   derive zone and type live from the location, but the Product IQC list,
   Lot detail, the inventory export and transfers read these stored columns.
   The tag's Location ID is its latest DeviceLocationLog row (what every page
   shows), else devices.location_id.

2. backfill_sold_invoice_once — tags currently in Sold get the Invoice Number
   of their latest sale (when that sale has one), matching what recording a
   sale now does. Each tag's previous invoice is kept in a per-tag audit row.
"""
from sqlalchemy import select, update, func, bindparam, text

from models.settings import AppSetting
from models.device import Device, DeviceStage
from models.location import StorageLocation
from models.sales import Sale
from services.audit_engine import audit

LOCATION_TEXT_MARKER = "tag_location_text_backfill_2026_10_21"
SOLD_INVOICE_MARKER = "sold_invoice_backfill_2026_10_21"
REPLACEMENT_INVOICE_MARKER = "replacement_invoice_backfill_2026_10_21"
_CHUNK = 5000


async def _done(db, key) -> bool:
    return (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none() is not None


def _mark(db, key, value, description):
    db.add(AppSetting(key=key, value=str(value)[:2000], updated_by="system", description=description[:200]))


async def backfill_tag_location_text_once(db) -> int | None:
    """Returns tags updated, or None when already done."""
    if await _done(db, LOCATION_TEXT_MARKER):
        return None
    # Effective location per tag: latest log, else the column.
    rows = (await db.execute(text("""
        SELECT d.id, COALESCE(l.location_id, d.location_id) AS loc_id
        FROM devices d
        LEFT JOIN (
            SELECT DISTINCT ON (device_id) device_id, location_id
            FROM device_location_logs
            ORDER BY device_id, logged_at DESC
        ) l ON l.device_id = d.id
        WHERE COALESCE(l.location_id, d.location_id) IS NOT NULL
    """))).all()
    by_loc: dict = {}
    for dev_id, loc_id in rows:
        by_loc.setdefault(loc_id, []).append(dev_id)
    locs = {l.id: l for l in (await db.execute(
        select(StorageLocation).where(StorageLocation.id.in_(list(by_loc)))
    )).scalars().all()} if by_loc else {}

    updated = 0
    for loc_id, dev_ids in by_loc.items():
        loc = locs.get(loc_id)
        if loc is None:
            continue
        zone, wh = loc.zone_label, loc.display_name
        zone = getattr(zone, "value", zone)
        for i in range(0, len(dev_ids), _CHUNK):
            res = await db.execute(
                update(Device)
                .where(Device.id.in_(dev_ids[i:i + _CHUNK]),
                       (Device.floor.is_distinct_from(zone)) | (Device.warehouse.is_distinct_from(wh)))
                .values(floor=zone, warehouse=wh)
                .execution_options(synchronize_session=False)
            )
            updated += res.rowcount or 0
    await audit(db, action="BACKFILL_TAG_LOCATION_TEXT", user=None, table_name="devices",
                record_id="backfill-2026-10-21",
                new_value={"tags_updated": updated, "tags_with_location": len(rows)})
    _mark(db, LOCATION_TEXT_MARKER, updated, "One-time: tag Zone / Location Type text from Location ID")
    await db.commit()
    return updated


async def backfill_sold_invoice_once(db) -> int | None:
    """Returns tags updated, or None when already done."""
    if await _done(db, SOLD_INVOICE_MARKER):
        return None
    # Latest sale per tag currently in Sold.
    latest = (
        select(Sale.device_id, Sale.invoice_no,
               func.row_number().over(partition_by=Sale.device_id,
                                      order_by=(Sale.sold_at.desc().nulls_last(), Sale.sale_number.desc())).label("rn"))
        .join(Device, Device.id == Sale.device_id)
        .where(Device.current_stage == DeviceStage.sold)
        .subquery()
    )
    rows = (await db.execute(
        select(latest.c.device_id, latest.c.invoice_no, Device.invoice_number, Device.barcode)
        .join(Device, Device.id == latest.c.device_id)
        .where(latest.c.rn == 1)
    )).all()
    changes = []
    for dev_id, sale_inv, cur_inv, barcode in rows:
        inv = (sale_inv or "").strip()
        if inv and inv != (cur_inv or "").strip():
            changes.append((dev_id, inv, cur_inv, barcode))

    _t = Device.__table__
    stmt = update(_t).where(_t.c.id == bindparam("b_id")).values(invoice_number=bindparam("b_inv"))
    for i in range(0, len(changes), _CHUNK):
        part = changes[i:i + _CHUNK]
        await db.execute(stmt, [{"b_id": c[0], "b_inv": c[1]} for c in part])
        for dev_id, inv, old, barcode in part:
            await audit(db, action="BACKFILL_SOLD_INVOICE", user=None, table_name="devices",
                        record_id=str(dev_id),
                        old_value={"barcode": barcode, "invoice_number": old},
                        new_value={"barcode": barcode, "invoice_number": inv})
    await audit(db, action="BACKFILL_SOLD_INVOICE_SUMMARY", user=None, table_name="devices",
                record_id="backfill-2026-10-21",
                new_value={"tags_updated": len(changes), "sold_tags_checked": len(rows)})
    _mark(db, SOLD_INVOICE_MARKER, len(changes), "One-time: Sold tags take their latest sale's Invoice Number")
    await db.commit()
    return len(changes)


async def backfill_replacement_invoice_once(db) -> int | None:
    """Tags given to a customer as a replacement ("Replaced with <tag>") take
    the Invoice Number the customer's original tag was sold on — its latest
    sale with an invoice. Previous values kept in per-tag audit rows.
    Returns tags updated, or None when already done."""
    if await _done(db, REPLACEMENT_INVOICE_MARKER):
        return None
    repl = (await db.execute(
        select(Device.id, Device.barcode, Device.invoice_number, Device.replaced)
        .where(Device.replaced.like("Replaced with %"))
    )).all()
    orig_of = {r.id: r.replaced[len("Replaced with "):].strip().upper() for r in repl if r.replaced}
    inv_by_tag: dict = {}
    if orig_of:
        tags = list(set(orig_of.values()))
        for i in range(0, len(tags), _CHUNK):
            for bc, inv in (await db.execute(
                select(func.upper(Device.barcode), Sale.invoice_no)
                .join(Device, Device.id == Sale.device_id)
                .where(func.upper(Device.barcode).in_(tags[i:i + _CHUNK]),
                       func.coalesce(func.trim(Sale.invoice_no), "") != "")
                .order_by(Sale.sold_at.desc())
            )).all():
                inv_by_tag.setdefault(bc, inv.strip())
    changes = []
    for r in repl:
        inv = inv_by_tag.get(orig_of.get(r.id, ""))
        if inv and inv != (r.invoice_number or "").strip():
            changes.append((r.id, inv, r.invoice_number, r.barcode, orig_of[r.id]))
    _t = Device.__table__
    stmt = update(_t).where(_t.c.id == bindparam("b_id")).values(invoice_number=bindparam("b_inv"))
    if changes:
        await db.execute(stmt, [{"b_id": c[0], "b_inv": c[1]} for c in changes])
    for dev_id, inv, old, barcode, orig in changes:
        await audit(db, action="BACKFILL_REPLACEMENT_INVOICE", user=None, table_name="devices",
                    record_id=str(dev_id),
                    old_value={"barcode": barcode, "invoice_number": old},
                    new_value={"barcode": barcode, "invoice_number": inv, "replaced_with": orig})
    await audit(db, action="BACKFILL_REPLACEMENT_INVOICE_SUM", user=None, table_name="devices",
                record_id="backfill-2026-10-21",
                new_value={"tags_updated": len(changes), "replacement_tags_checked": len(repl)})
    _mark(db, REPLACEMENT_INVOICE_MARKER, len(changes),
          "One-time: replacement tags take the customer's original invoice")
    await db.commit()
    return len(changes)
