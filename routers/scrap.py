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
               Device.is_trashed == False)
        .order_by(Device.updated_at.desc())
    )).all()
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
        })

    # Scrapped Spare Parts — faulty parts marked Scrap on the Parts Dashboard's
    # Faulty Request tab (routers/part_requests.py :: scrap_part_request).
    scrapped_parts = (await db.execute(
        select(PartRequest).where(PartRequest.status == "scrapped")
        .order_by(PartRequest.actioned_at.desc())
    )).scalars().all()

    return templates.TemplateResponse("scrap/list.html", {
        "request": request, "current_user": current_user, "items": items,
        "scrapped_parts": scrapped_parts,
    })


@router.post("/scrap/{barcode}/verify")
async def verify_scrap(
    barcode: str,
    verified_qty: int = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Mark a scrapped device as quantity-verified. Enables the Sell button."""
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if device is None:
        return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)

    device.scrap_verified = True
    if notes:
        existing = device.notes or ""
        device.notes = (existing + "\n[Verify] " + notes).strip()

    await db.commit()
    return JSONResponse({"ok": True})


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
