"""
Trash — soft-delete for Lots and Devices.
Trashed items are hidden from main lists and shown here.
Admin can restore or request permanent deletion.
"""
from templates_config import templates
import logging
import uuid as _uuid
from datetime import datetime
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, exists, or_, delete as sa_delete
from sqlalchemy.orm import aliased

from database import get_db
from models.user import User, UserRole
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Sale, Return
from auth.dependencies import require_roles, verify_csrf
from services.audit_engine import audit
from utils.fk_purge import purge_references

router = APIRouter(prefix="/trash", tags=["trash"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager)

_log = logging.getLogger("oxypc.errors")


def _confirmed(typed: str, expected: str) -> bool:
    """Permanent delete is the only action in the app with no undo, so the
    button alone is not enough — the operator has to type the lot number or tag
    number back. Case- and space-insensitive, because the point is deliberate
    intent, not transcription accuracy."""
    return (typed or "").strip().casefold() == (expected or "").strip().casefold()


def _has_live_twin():
    """A trashed device whose Tag Number differs from a LIVE device's only in
    letter case (e.g. trashed "...l0647" vs live "...L0647") — the duplicate
    rows an IQC upload made with lowercase tags (2026-10-17)."""
    live = aliased(Device)
    return exists().where(
        func.upper(live.barcode) == func.upper(Device.barcode),
        live.id != Device.id,
        live.is_trashed.isnot(True),
    )


def _has_sale_or_return():
    """sales.device_id / returns.device_id are NOT NULL, so a permanent delete
    would take these records with the device — such duplicates are kept."""
    return or_(exists().where(Sale.device_id == Device.id),
               exists().where(Return.device_id == Device.id))


@router.get("", response_class=HTMLResponse)
async def trash_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Show all trashed lots and trashed devices."""
    trashed_lots = (await db.execute(
        select(Lot).where(Lot.is_trashed == True).order_by(Lot.trashed_at.desc())
    )).scalars().all()

    trashed_devices = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.is_trashed == True)
        .order_by(Device.trashed_at.desc())
    )).all()

    dup_rows = (await db.execute(
        select(Device.id, _has_sale_or_return().label("has_sale"))
        .where(Device.is_trashed == True, _has_live_twin())  # noqa: E712
    )).all()
    dup_ids = {str(r.id) for r in dup_rows}
    dup_kept = sum(1 for r in dup_rows if r.has_sale)
    # Trashed tags that still carry a sale or return record — badged, and
    # the "With sale/return only" checkbox filters to them (2026-10-22).
    sale_ret_ids = {str(i) for i in (await db.execute(
        select(Device.id).where(Device.is_trashed == True, _has_sale_or_return())  # noqa: E712
    )).scalars().all()}

    return templates.TemplateResponse("trash/index.html", {
        "request": request,
        "current_user": current_user,
        "trashed_lots": trashed_lots,
        "trashed_devices": trashed_devices,
        "dup_ids": dup_ids,
        "dup_deletable": len(dup_rows) - dup_kept,
        "dup_kept": dup_kept,
        "sale_ret_ids": sale_ret_ids,
    })


@router.post("/duplicates/delete", response_class=HTMLResponse)
async def delete_duplicate_devices(
    request: Request,
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Permanently delete every trashed device that is a different-letter-case
    duplicate of a live device (see _has_live_twin), in one go.

    Same safeguards as the single delete: trashed rows only, typed
    confirmation, FK-catalog cascade, audit before the rows go. Duplicates with
    their own sale or return are skipped (deleting them would delete that
    sale/return). Committed in batches so one huge transaction can't hit the
    statement timeout; a batch that a foreign key blocks is rolled back and
    reported, and earlier batches stay deleted."""
    from urllib.parse import quote
    from sqlalchemy.exc import IntegrityError

    if confirm.strip().upper() != "DELETE DUPLICATES":
        return RedirectResponse(
            url="/trash?tab=tags&error=" + quote("Type DELETE DUPLICATES exactly to confirm"),
            status_code=302)

    rows = (await db.execute(
        select(Device.id, Device.barcode)
        .where(Device.is_trashed == True, _has_live_twin(), ~_has_sale_or_return())  # noqa: E712
        .order_by(Device.barcode)
    )).all()
    if not rows:
        return RedirectResponse(
            url="/trash?tab=tags&error=" + quote("No duplicate Tag Numbers to delete"), status_code=302)

    deleted = 0
    BATCH = 200
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        ids = [r.id for r in chunk]
        tags = [r.barcode for r in chunk]
        try:
            purged = await purge_references(db, "devices", ids)
            await audit(
                db, action="DEVICE_DUPLICATES_DELETED", user=current_user,
                table_name="devices", record_id=None,
                new_value={"count": len(ids), "barcodes": tags, "from": "trash",
                           "reason": "letter-case duplicate of a live Tag Number",
                           "deleted_by": current_user.username, "cascade": purged},
                request=request,
            )
            await db.execute(sa_delete(Device).where(Device.id.in_(ids), Device.is_trashed == True))  # noqa: E712
            await db.commit()
        except IntegrityError as e:
            await db.rollback()
            _log.exception("bulk duplicate delete blocked by a foreign key (batch from %s)", tags[0])
            detail = getattr(getattr(e, "orig", None), "constraint_name", None) or "a related record"
            return RedirectResponse(
                url="/trash?tab=tags&error=" + quote(
                    f"{deleted} duplicate(s) deleted, then stopped — a batch is still referenced by {detail}"),
                status_code=302)
        deleted += len(ids)

    return RedirectResponse(
        url="/trash?tab=tags&success=" + quote(f"{deleted} duplicate Tag Number(s) deleted permanently"),
        status_code=302)


# ── Lots ─────────────────────────────────────────────────────────────────────

@router.post("/lots/{lot_id}", response_class=HTMLResponse)
async def trash_lot(
    lot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Move a lot to trash."""
    try:
        uid = _uuid.UUID(lot_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Lot not found")

    lot = (await db.execute(select(Lot).where(Lot.id == uid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")

    lot.is_trashed = True
    lot.trashed_at = app_now()
    await db.commit()
    return RedirectResponse(url="/lots?success=Lot+moved+to+trash", status_code=302)


@router.post("/lots/{lot_id}/restore", response_class=HTMLResponse)
async def restore_lot(
    lot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Restore a lot from trash."""
    try:
        uid = _uuid.UUID(lot_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Lot not found")

    lot = (await db.execute(select(Lot).where(Lot.id == uid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")

    lot.is_trashed = False
    lot.trashed_at = None
    await db.commit()
    return RedirectResponse(url="/trash?tab=lots&success=Lot+restored", status_code=302)


@router.post("/lots/{lot_id}/delete", response_class=HTMLResponse)
async def delete_lot_forever(
    lot_id: str,
    request: Request,
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Permanently delete a trashed lot, its devices, and everything that
    references them.

    Only a lot already IN the trash can be deleted here, so nothing can be
    destroyed straight off a working list — it has to be trashed first, seen in
    this page, and then confirmed by name. The cascade comes from the database's
    own foreign keys at request time (utils/fk_purge) rather than a hand-kept
    table list, which is what stopped 22 of 287 production lots from deleting
    when the list fell behind the schema.
    """
    from urllib.parse import quote
    from sqlalchemy import text as _text
    from sqlalchemy.exc import IntegrityError

    try:
        uid = _uuid.UUID(lot_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Lot not found")

    lot = (await db.execute(select(Lot).where(Lot.id == uid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail="Lot not found")
    if not lot.is_trashed:
        return RedirectResponse(
            url="/trash?tab=lots&error=" + quote("Only a lot already in Trash can be deleted permanently"),
            status_code=302)
    if not _confirmed(confirm, lot.lot_number):
        return RedirectResponse(
            url="/trash?tab=lots&error=" + quote(f"Type {lot.lot_number} exactly to confirm deletion"),
            status_code=302)

    lot_number = lot.lot_number
    params = {"lot_id": str(uid)}

    device_ids = (await db.execute(
        _text("SELECT id FROM devices WHERE lot_id = :lot_id"), params)).scalars().all()
    purged = await purge_references(db, "devices", device_ids)
    if device_ids:
        await db.execute(_text("DELETE FROM devices WHERE lot_id = :lot_id"), params)
        purged.append(f"deleted {len(device_ids)} devices")
    purged += await purge_references(db, "lots", [uid])

    # Audited before the row goes, so the trail survives the record.
    await audit(
        db, action="LOT_DELETED", user=current_user,
        table_name="lots", record_id=str(uid),
        new_value={"lot_number": lot_number, "supplier": lot.supplier_name,
                   "buying_price": str(lot.buying_price), "qty": lot.qty,
                   "from": "trash", "deleted_by": current_user.username,
                   "cascade": purged},
        request=request,
    )

    await db.delete(lot)
    try:
        await db.commit()
    except IntegrityError as e:
        # Something the purge could not resolve. Leave the lot intact and name
        # the constraint instead of returning a 500 the user cannot act on.
        await db.rollback()
        _log.exception("permanent lot delete blocked by a foreign key: %s", lot_number)
        detail = getattr(getattr(e, "orig", None), "constraint_name", None) or "a related record"
        return RedirectResponse(
            url="/trash?tab=lots&error=" + quote(
                f"{lot_number} could not be deleted — still referenced by {detail}"),
            status_code=302)

    return RedirectResponse(
        url="/trash?tab=lots&success=" + quote(
            f"{lot_number} and its {len(device_ids)} device(s) deleted permanently"),
        status_code=302)


# ── Devices ───────────────────────────────────────────────────────────────────

@router.post("/devices/{barcode}", response_class=HTMLResponse)
async def trash_device(
    barcode: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Move a device to trash."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode)
    )).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device.is_trashed = True
    device.trashed_at = app_now()
    await db.commit()
    return RedirectResponse(url="/devices?success=Device+moved+to+trash", status_code=302)


@router.post("/bulk/devices", response_class=HTMLResponse)
async def trash_devices_bulk(
    request: Request,
    barcodes: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Bulk soft-delete (move to Trash) the selected devices. Restorable from /trash."""
    barcodes = [b for b in (barcodes or []) if b]
    if not barcodes:
        return RedirectResponse(url="/devices?error=No+devices+selected", status_code=302)
    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(barcodes), Device.is_trashed == False)
    )).scalars().all()
    now = app_now()
    for d in devices:
        d.is_trashed = True
        d.trashed_at = now
    await db.commit()
    return RedirectResponse(
        url=f"/devices?success={len(devices)}+device(s)+moved+to+trash", status_code=302)


@router.post("/devices/{barcode}/delete", response_class=HTMLResponse)
async def delete_device_forever(
    barcode: str,
    request: Request,
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Permanently delete one trashed device and everything referencing it.

    Same shape as the lot delete above: trashed-only, confirmed by tag number,
    cascade read from the FK catalog. Its IQC inspection, stage movements,
    parts consumption and scan lines go with it; records that merely mention
    the device are unlinked, not destroyed.
    """
    from urllib.parse import quote
    from sqlalchemy.exc import IntegrityError

    device = (await db.execute(
        select(Device).where(Device.barcode == barcode)
    )).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.is_trashed:
        return RedirectResponse(
            url="/trash?error=" + quote("Only a device already in Trash can be deleted permanently"),
            status_code=302)
    if not _confirmed(confirm, device.barcode):
        return RedirectResponse(
            url="/trash?error=" + quote(f"Type {device.barcode} exactly to confirm deletion"),
            status_code=302)

    tag = device.barcode
    purged = await purge_references(db, "devices", [device.id])

    await audit(
        db, action="DEVICE_DELETED", user=current_user,
        table_name="devices", record_id=str(device.id),
        new_value={"barcode": tag, "brand": device.brand, "model": device.model,
                   "grade": device.grade.value if device.grade else None,
                   "stage": device.current_stage.value if device.current_stage else None,
                   "lot_id": str(device.lot_id) if device.lot_id else None,
                   "from": "trash", "deleted_by": current_user.username,
                   "cascade": purged},
        request=request,
    )

    await db.delete(device)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        _log.exception("permanent device delete blocked by a foreign key: %s", tag)
        detail = getattr(getattr(e, "orig", None), "constraint_name", None) or "a related record"
        return RedirectResponse(
            url="/trash?error=" + quote(
                f"{tag} could not be deleted — still referenced by {detail}"),
            status_code=302)

    return RedirectResponse(
        url="/trash?success=" + quote(f"{tag} deleted permanently"), status_code=302)


@router.post("/devices/{barcode}/restore", response_class=HTMLResponse)
async def restore_device(
    barcode: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Restore a device from trash."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode)
    )).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device.is_trashed = False
    device.trashed_at = None
    await db.commit()
    return RedirectResponse(url="/trash?success=Device+restored", status_code=302)
