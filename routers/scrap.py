"""
Scrap Products — devices that were scrapped (typically from L3). Shows the L3
engineer who sent it to scrap, the device's P&L total cost, and Sell / View actions.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from templates_config import templates
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, DeviceGrade, StageMovement
from models.lot import Lot
from models.engines import DeviceCosting
from models.part_request import PartRequest
from auth.dependencies import get_current_user, require_roles, verify_csrf

router = APIRouter(tags=["scrap"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.l3_engineer,
                        UserRole.sales, UserRole.sales_manager)


@router.get("/scrap-products", response_class=HTMLResponse)
async def scrap_products(request: Request, db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(allowed)):
    rows = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        # Every tag in the Scrap for Sale stage, whatever its grade (2026-10-19)
        # — same set as the Scrap for Sale page. It used to require
        # grade == Scrap too, so tags moved there by a bulk stage move with
        # grade A/B/C (or none) were missing here (185 shown vs 469).
        .where(Device.current_stage == DeviceStage.scrap_for_sale,
               Device.is_trashed.isnot(True))
        .order_by(Device.updated_at.desc())
    )).all()
    # A tag leaves Tags Scrapped once it is in a live Scrap ID batch (made
    # through Transfers) — from then on it is only on Scrap for Sale.
    from services.scrap_flow import batched_device_ids, can_verify
    _in_batch = await batched_device_ids(db, [d.id for d, _ in rows])
    rows = [(d, ln) for d, ln in rows if d.id not in _in_batch]
    device_ids = [d.id for d, _ in rows]

    # Amount = device P&L total cost (DeviceCosting); fallback to device_price * qty
    cost_map = {}
    received_map = {}
    if device_ids:
        for c in (await db.execute(
            select(DeviceCosting).where(DeviceCosting.device_id.in_(device_ids))
        )).scalars().all():
            cost_map[str(c.device_id)] = c.total_cost

        # L3 engineer who sent it to scrap = moved_by on the latest movement to 'scrapped'
        sub = (
            select(StageMovement.device_id, func.max(StageMovement.moved_at).label("latest"))
            .where(StageMovement.device_id.in_(device_ids),
                   StageMovement.to_stage == DeviceStage.scrapped)
            .group_by(StageMovement.device_id).subquery()
        )
        mv_rows = (await db.execute(
            select(StageMovement.device_id, StageMovement.moved_by, StageMovement.notes)
            .join(sub, (StageMovement.device_id == sub.c.device_id) &
                       (StageMovement.moved_at == sub.c.latest))
            .where(StageMovement.to_stage == DeviceStage.scrapped)
        )).all()
        for did, mb, nt in mv_rows:
            received_map[str(did)] = mb

    items = []
    for device, lot_number in rows:
        did = str(device.id)
        amount = cost_map.get(did)
        if amount is None and device.device_price:
            amount = device.device_price * (device.qty or 1)
        items.append({
            "barcode": device.barcode,
            "brand": device.brand or "—",
            "model": device.model or "—",
            "grade": device.grade.value if device.grade else "—",
            "received_user": received_map.get(did) or "—",
            "amount": amount,
            "updated_at": device.updated_at,
            "notes": device.notes or "",
            "scrap_verified": device.scrap_verified,
            "l34_status": device.l34_status or "",
            "replace_with_barcode": device.replace_with_barcode or "",
            "can_verify": can_verify(device),
        })

    # Scrapped Spare Parts — faulty parts marked Scrap on the Parts Dashboard's
    # Faulty Request tab (routers/part_requests.py :: scrap_part_request).
    scrapped_parts = (await db.execute(
        select(PartRequest).where(PartRequest.status == "scrapped")
        .order_by(PartRequest.actioned_at.desc())
    )).scalars().all()

    # No Sell here any more (2026-11-09): Verify sends tags to a Scrap ID on
    # Scrap for Sale, and selling happens only there.
    return templates.TemplateResponse("scrap/list.html", {
        "request": request, "current_user": current_user, "items": items,
        "scrapped_parts": scrapped_parts,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/scrap/{barcode}/verify")
async def verify_scrap(
    barcode: str,
    request: Request,
    verified_qty: int = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Verify one tag on Tags Scrapped: marks it received AND puts it into a
    new Scrap ID on Scrap for Sale, like a Transfers "Scrap for Sale"
    submission (2026-11-09). It then leaves Tags Scrapped; it is sold from
    Scrap for Sale only."""
    from services.audit_engine import audit
    from services.scrap_flow import batched_device_ids, verify_eligible, verify_into_scrap_batch
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if device is None:
        return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)
    in_batch = await batched_device_ids(db, [device.id])
    if not verify_eligible(device, in_batch):
        why = (f"already in Scrap ID {in_batch[device.id]}" if device.id in in_batch
               else "set its replacement tag first" if (device.l34_status or "") == "Replacement Scrap"
               and not device.replace_with_barcode else "not on Tags Scrapped")
        return JSONResponse({"ok": False, "error": f"{device.barcode}: {why}."}, status_code=400)
    batch = await verify_into_scrap_batch(db, [device], current_user, notes)
    await audit(db, user=current_user, action="SCRAP_VERIFIED", table_name="devices",
                record_id=str(device.id),
                new_value={"scrap_verified": True, "scrap_id": batch.scrap_id, "notes": notes.strip() or None},
                notes=f"Verified on Tags Scrapped -> Scrap ID {batch.scrap_id}", request=request)
    await db.commit()
    return JSONResponse({"ok": True, "scrap_id": batch.scrap_id})


@router.post("/scrap/bulk-verify")
async def bulk_verify_scrap(
    request: Request,
    barcodes: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Tags Scrapped page "Verify (n)": verifies the ticked tags and puts them
    all into ONE new Scrap ID on Scrap for Sale (2026-11-09) — one Scrap ID per
    Verify, like one Transfers submission. Only tags the row Verify would act
    on are taken (see services.scrap_flow.verify_eligible); the rest are
    reported as skipped."""
    from services.audit_engine import audit
    from services.scrap_flow import batched_device_ids, verify_eligible, verify_into_scrap_batch
    wanted, seen = [], set()
    for b in barcodes.replace("\n", ",").split(","):
        b = b.strip()
        if b and b.upper() not in seen:
            seen.add(b.upper())
            wanted.append(b)
    if not wanted:
        return JSONResponse({"ok": False, "error": "Select at least one tag."}, status_code=400)
    devices = (await db.execute(
        select(Device).where(func.upper(Device.barcode).in_([w.upper() for w in wanted]))
    )).scalars().all()
    by_upper = {d.barcode.upper(): d for d in devices}
    in_batch = await batched_device_ids(db, [d.id for d in devices])
    take, skipped = [], []
    for w in wanted:
        d = by_upper.get(w.upper())
        if verify_eligible(d, in_batch):
            take.append(d)
        else:
            skipped.append(w)
    batch = await verify_into_scrap_batch(db, take, current_user, notes)
    done = [d.barcode for d in take]
    if batch:
        await audit(db, user=current_user, action="SCRAP_BULK_VERIFIED", table_name="devices",
                    record_id=f"{len(done)} tags",
                    new_value={"scrap_verified": True, "scrap_id": batch.scrap_id, "barcodes": done,
                               "notes": notes.strip() or None},
                    notes=f"Bulk Verify on Tags Scrapped: {len(done)} tag(s) -> Scrap ID {batch.scrap_id}",
                    request=request)
    await db.commit()
    return JSONResponse({"ok": True, "verified": done, "skipped": skipped,
                         "scrap_id": batch.scrap_id if batch else None})


@router.post("/scrap-products/{barcode}/set-replacement")
async def set_scrap_replacement(
    barcode: str,
    replacement_barcode: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Replacement Scrap flow — the 'Replace' button's modal. Records the
    selected replacement tag on the scrapped device (Device.replace_with_barcode)
    so the Scrap Products table can show '(Replace with <tag>)' beneath the
    scrapped tag number. Does not modify the replacement device itself — it is
    only referenced/clickable, unlike the symmetric L3/TRC 'replaced' swap."""
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if device is None:
        return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)

    rb = (replacement_barcode or "").strip()
    replacement = (await db.execute(select(Device).where(Device.barcode == rb))).scalar_one_or_none()
    if replacement is None:
        return JSONResponse({"ok": False, "error": f"Tag {rb} not found"}, status_code=404)

    device.replace_with_barcode = replacement.barcode
    await db.commit()
    return JSONResponse({"ok": True, "replace_with_barcode": replacement.barcode})
