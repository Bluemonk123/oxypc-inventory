"""
Parts request → handover → sourcing workflow.

 - Engineer raises a request from the device Parts Consumption section.
 - Spare Parts Manager actions it on the Part Master page: Handover / Not In Stock / Procure.
 - Procure creates a sourcing request, closed by the Sales Manager in the CRM Dashboard.
"""
import json
import uuid
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func

from database import get_db
from models.user import User, UserRole
from models.device import Device
from models.work_order import WorkOrder
from models.spare_parts import SparePart
from models.engines import SparePartsLedger
from models.part_request import PartRequest, PartSourcingRequest
from models.pna_part import DevicePNAPart
from auth.dependencies import get_current_user, require_roles, verify_csrf
from services.audit_engine import audit
from services.work_order_close import wo_open  # open = not completed / reassigned

router = APIRouter(tags=["part_requests"], dependencies=[Depends(verify_csrf)])

eng_allowed = require_roles(UserRole.admin, UserRole.inventory_manager,
                            UserRole.l1_engineer, UserRole.l2_engineer, UserRole.l3_engineer)
spm_allowed = require_roles(UserRole.admin, UserRole.spare_parts_manager)
sm_allowed = require_roles(UserRole.admin, UserRole.sales_manager)


def _as_uuid(val):
    try:
        return uuid.UUID(val)
    except Exception:
        return None


@router.post("/part-requests/create")
async def create_part_request(
    request: Request,
    barcode: str = Form(...),
    part_name: str = Form(...),
    part_id: str = Form(""),
    part_category: str = Form(""),
    part_capacity: str = Form(""),
    part_type: str = Form(""),
    request_type: str = Form("new"),
    downgrade_type: str = Form(""),
    part_make: str = Form(""),
    part_model: str = Form(""),
    qty: int = Form(1),
    from_page: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # any signed-in user (2026-10-26): Parts Consumption is outside the Permission Matrix
):
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    wo = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device.id, wo_open())
        .order_by(WorkOrder.assigned_at.desc())
    )).scalars().first()
    stage = device.current_stage.value if device.current_stage else None
    if stage not in ("l1", "l2", "l3"):
        stage = wo.stage if wo else None

    # ── Resolve part_id to a real SparePart (BUG 3) ───────────────────────────
    # The spare-parts fulfilment page looks up stock by PartRequest.part_id, and
    # the repair/detail "In Stock" display shows the stock of the SparePart it
    # matched. If those diverge (or part_id arrives empty because a category→
    # name cascade came up blank), spare-parts shows 0 while repair shows stock.
    # Prefer the part_id the form sent (the exact record whose stock was shown);
    # otherwise fall back to the same fuzzy match the display uses so a real
    # SparePart id is stored rather than None.
    resolved_part_id = _as_uuid(part_id)
    if resolved_part_id:
        exists = (await db.execute(
            select(SparePart.id).where(SparePart.id == resolved_part_id)
        )).scalar_one_or_none()
        if not exists:
            resolved_part_id = None
    if not resolved_part_id:
        conds = [SparePart.name.ilike(f"%{part_name}%")]
        if part_category.strip():
            conds.append(SparePart.category == part_category.strip())
        sp_match = (await db.execute(
            select(SparePart).where(or_(*conds)).order_by(SparePart.qty_in_stock.desc())
        )).scalars().first()
        if sp_match:
            resolved_part_id = sp_match.id

    pr = PartRequest(
        work_order_id=wo.id if wo else None,
        work_id=wo.work_id if wo else None,
        device_id=device.id, barcode=device.barcode, stage=stage,
        part_id=resolved_part_id, part_name=part_name,
        part_category=part_category.strip() or None,
        part_capacity=part_capacity.strip() or None,
        part_type=part_type.strip() or None,
        request_type=request_type.strip() or "new",
        downgrade_type=downgrade_type.strip() or None,
        part_make=part_make.strip() or None,
        part_model=part_model.strip() or None,
        requested_by=current_user.username, engineer_name=current_user.full_name,
        qty_requested=max(1, qty), status="requested",
    )
    db.add(pr)
    await audit(db, user=current_user, action="PART_REQUESTED", table_name="part_requests",
                record_id=None, new_value={"barcode": barcode, "part": part_name, "qty": qty},
                request=request)
    await db.commit()
    suffix = f"&from={from_page}" if from_page else ""
    return RedirectResponse(url=f"/devices/{barcode}?success=Part+request+raised+for+{part_name}{suffix}",
                            status_code=302)


@router.post("/part-requests/multi-create")
async def create_part_requests_batch(
    request: Request,
    barcode: str = Form(...),
    parts_json: str = Form(...),
    request_type: str = Form("new"),
    from_page: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # any signed-in user (2026-10-26): Parts Consumption is outside the Permission Matrix
):
    """Device Detail Parts Consumption's "Multi Request" button: check several
    rows, then in the Assign & Move-to-Cleaning-style modal pick a Make/Model/
    Quantity per row (Part Name comes pre-filled from the checked row) under
    either the New Request or Replace Request tab, and raise every row as one
    batch under that tab's type. Each row's label/category/part_id/make/
    model/qty travels as one JSON array rather than indexed form fields,
    since there's no existing PartRequest row per item to reference by id the
    way bulk_request_action's `ids` list does — these are brand new rows
    being created, not existing ones being actioned.

    Creates one PartRequest per selected part with the same field resolution
    create_part_request() uses (including the part_id -> SparePart fuzzy-
    match fallback), so every request lands on Parts Manager's Part Requests
    tab exactly the same way a single request does.
    """
    try:
        parts = json.loads(parts_json)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid parts payload")
    if not isinstance(parts, list) or not parts:
        raise HTTPException(400, "No parts selected")

    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    wo = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device.id, wo_open())
        .order_by(WorkOrder.assigned_at.desc())
    )).scalars().first()
    stage = device.current_stage.value if device.current_stage else None
    if stage not in ("l1", "l2", "l3"):
        stage = wo.stage if wo else None

    rtype = (request_type or "new").strip() or "new"
    raised = []
    for item in parts:
        if not isinstance(item, dict):
            continue
        part_name = (item.get("label") or "").strip()
        if not part_name:
            continue
        part_category = (item.get("category") or "").strip()
        part_make = (item.get("make") or "").strip()
        part_model = (item.get("model") or "").strip()
        try:
            qty = max(1, int(item.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1

        # Same part_id resolution as create_part_request (BUG 3) — prefer the
        # id the row already matched (the modal's Name+Make+Model cascade
        # resolves this client-side, same as the single-request modal), fall
        # back to a fuzzy name/category match.
        resolved_part_id = _as_uuid(item.get("part_id") or "")
        if resolved_part_id:
            exists = (await db.execute(
                select(SparePart.id).where(SparePart.id == resolved_part_id)
            )).scalar_one_or_none()
            if not exists:
                resolved_part_id = None
        if not resolved_part_id:
            conds = [SparePart.name.ilike(f"%{part_name}%")]
            if part_category:
                conds.append(SparePart.category == part_category)
            sp_match = (await db.execute(
                select(SparePart).where(or_(*conds)).order_by(SparePart.qty_in_stock.desc())
            )).scalars().first()
            if sp_match:
                resolved_part_id = sp_match.id

        db.add(PartRequest(
            work_order_id=wo.id if wo else None,
            work_id=wo.work_id if wo else None,
            device_id=device.id, barcode=device.barcode, stage=stage,
            part_id=resolved_part_id, part_name=part_name,
            part_category=part_category or None,
            part_make=part_make or None, part_model=part_model or None,
            request_type=rtype,
            requested_by=current_user.username, engineer_name=current_user.full_name,
            qty_requested=qty, status="requested",
        ))
        raised.append(part_name)

    if not raised:
        raise HTTPException(400, "No valid parts to request")

    await audit(db, user=current_user, action="PART_REQUESTED_MULTI", table_name="part_requests",
                record_id=None, new_value={"barcode": barcode, "parts": raised, "count": len(raised)},
                request=request)
    await db.commit()
    suffix = f"&from={from_page}" if from_page else ""
    return RedirectResponse(
        url=f"/devices/{barcode}?success={len(raised)}+part+request(s)+raised{suffix}",
        status_code=302)


async def _handover_options(db, reqs) -> list:
    """Per request: the Part Master crate rows it can be handed over from —
    every live row with the request's Part Name (New and Harvest, any
    Make / Model), each with its live count."""
    from services.parts_stock import live_map, norm
    names = {norm(r.part_name) for r in reqs if norm(r.part_name)}
    rows = []
    if names:
        rows = (await db.execute(select(SparePart).where(
            SparePart.is_trashed.isnot(True),
            func.lower(func.trim(SparePart.name)).in_(names)))).scalars().all()
    live = await live_map(db, rows)
    out = []
    for r in reqs:
        mine = [p for p in rows if norm(p.name) == norm(r.part_name)]
        mine.sort(key=lambda p: (-live[str(p.id)], norm(p.crate_number)))
        out.append({
            "id": str(r.id), "part": r.part_name, "eng": r.engineer_name or r.requested_by or "",
            "qty": int(r.qty_requested or 0), "status": r.status, "barcode": r.barcode or "",
            "rows": [{"id": str(p.id), "crate": p.crate_number or "", "make": p.make or "",
                      "model": p.model or "", "source": "harvest" if p.source == "harvest" else "new",
                      "qty": live[str(p.id)]} for p in mine],
        })
    return out


@router.get("/part-requests/handover-options")
async def handover_options(request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(spm_allowed)):
    """Feeds the Handover modal (row button and Handover Selected alike)."""
    ids = [u for u in (_as_uuid(v) for v in request.query_params.getlist("ids")) if u]
    reqs = (await db.execute(select(PartRequest).where(PartRequest.id.in_(ids)))).scalars().all() if ids else []
    order = {u: i for i, u in enumerate(ids)}
    reqs.sort(key=lambda r: order.get(r.id, 0))
    return JSONResponse({"requests": await _handover_options(db, reqs)})


async def _do_handover(db, pr, row_id, current_user, request):
    """Hand one request over from one crate row (2026-10-31). Returns an
    error string, or None when done. Quantity always equals what was
    requested. In row stock mode the crate row's count is decremented here;
    in legacy mode (Parts Data Fix not yet applied) live stock still nets
    handovers by group, so the row is only linked."""
    from services.parts_stock import live_map, norm, row_mode
    if pr.status != "requested":
        return "already actioned"
    qty = max(0, int(pr.qty_requested or 0))
    row = None
    if row_id:
        row = (await db.execute(select(SparePart).where(
            SparePart.id == _as_uuid(row_id), SparePart.is_trashed.isnot(True)))).scalar_one_or_none()
        if row is None:
            return "crate row not found"
        if norm(row.name) != norm(pr.part_name):
            return f"crate row is {row.name}, request is {pr.part_name}"
    rowmode = await row_mode(db)
    if row is None:
        if rowmode:
            return "choose New / Harvest and a crate"
    else:
        avail = (await live_map(db, [row]))[str(row.id)]
        if avail < qty:
            return f"crate {row.crate_number or '—'} has only {avail}"
        if rowmode:
            row.qty_in_stock = int(row.qty_in_stock or 0) - qty
        pr.part_id = row.id
    pr.qty_handed_over = qty
    pr.status = "handed_over"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    nv = {"qty_handed_over": qty}
    if row is not None:
        nv.update({"part_code": row.part_code, "type": "Harvest" if row.source == "harvest" else "New",
                   "crate": row.crate_number, "make": row.make, "model": row.model,
                   "unit_price": str(row.unit_price or 0), "stock_mode": "row" if rowmode else "legacy"})
    await audit(db, user=current_user, action="PART_HANDOVER", table_name="part_requests",
                record_id=str(pr.id), new_value=nv, request=request)
    return None


@router.post("/part-requests/{req_id}/handover")
async def handover_part(req_id: str, request: Request,
                        part_row_id: str = Form(""),
                        db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    err = await _do_handover(db, pr, part_row_id, current_user, request)
    if err:
        from urllib.parse import quote_plus
        return RedirectResponse(url="/spare-parts?error=" + quote_plus("Handover not done: " + err), status_code=302)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Part+handed+over", status_code=302)


@router.post("/part-requests/{req_id}/validate-receiving")
async def validate_receiving(req_id: str, request: Request,
                             db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Engineer verifies a handed-over part on the Device Profile page —
    flips status to 'received' ('Changed' pill client-side). Called via
    fetch() (no form body), so it returns JSON rather than redirecting."""
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    pr.status = "received"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    await audit(db, user=current_user, action="PART_RECEIVED", table_name="part_requests",
                record_id=str(pr.id), request=request)
    # Verified on a tag already past Final QC (Pass Hold / Ready to Sale):
    # no Final QC pass is coming, so RAM/Hard Drive is written now (2026-10-15).
    if pr.device_id:
        from services.parts_changed import apply_spec_changes_if_verified_late
        dev = (await db.execute(select(Device).where(Device.id == pr.device_id))).scalar_one_or_none()
        if dev:
            await apply_spec_changes_if_verified_late(db, dev, current_user, request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/part-requests/{req_id}/not-in-stock")
async def not_in_stock(req_id: str, request: Request,
                       db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    pr.status = "not_in_stock"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    await audit(db, user=current_user, action="PART_NOT_IN_STOCK", table_name="part_requests",
                record_id=str(pr.id), request=request)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Marked+not+in+stock", status_code=302)


@router.post("/part-requests/{req_id}/procure")
async def procure_part(req_id: str, request: Request,
                       db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    part = None
    if pr.part_id:
        part = (await db.execute(select(SparePart).where(SparePart.id == pr.part_id))).scalar_one_or_none()
    pr.status = "procure"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    db.add(PartSourcingRequest(
        part_request_id=pr.id, part_id=pr.part_id,
        part_code=part.part_code if part else None, part_name=pr.part_name,
        qty_requested=pr.qty_requested, raised_by=current_user.username, status="open",
    ))
    await audit(db, user=current_user, action="PART_PROCURE", table_name="part_sourcing_requests",
                record_id=str(pr.id), new_value={"part": pr.part_name, "qty": pr.qty_requested},
                request=request)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Sent+to+sourcing", status_code=302)


@router.post("/part-requests/{req_id}/scrap")
async def scrap_part_request(req_id: str, request: Request,
                             db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    """Scrap button on the Faulty Request tab — a faulty part that isn't
    worth sourcing/handing over. Surfaces on the Scrap Products page's
    'Scrapped Spare Parts' table."""
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    pr.status = "scrapped"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    await audit(db, user=current_user, action="PART_SCRAPPED", table_name="part_requests",
                record_id=str(pr.id), new_value={"part": pr.part_name}, request=request)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Part+scrapped", status_code=302)


@router.post("/part-requests/{req_id}/warranty-replace")
async def warranty_replace(req_id: str, request: Request,
                           db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    """Warranty Faulty Replacement Cycle, step 1: a faulty part still under
    supplier warranty is sent back for replacement instead of scrap/sourcing."""
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    pr.status = "warranty_sent"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    await audit(db, user=current_user, action="PART_WARRANTY_SENT", table_name="part_requests",
                record_id=str(pr.id), new_value={"part": pr.part_name}, request=request)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Part+sent+for+warranty+replacement", status_code=302)


@router.post("/part-requests/{req_id}/warranty-received")
async def warranty_received(req_id: str, request: Request,
                            db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    """Warranty cycle, step 2: the replacement part arrived from the supplier —
    close the cycle and put the replacement back into spare-part stock."""
    pr = (await db.execute(select(PartRequest).where(PartRequest.id == _as_uuid(req_id)))).scalar_one_or_none()
    if not pr:
        raise HTTPException(404, "Part request not found")
    if pr.status != "warranty_sent":
        raise HTTPException(409, "Part is not in the warranty-replacement cycle")
    pr.status = "warranty_replaced"
    pr.actioned_at = app_now()
    pr.actioned_by = current_user.username
    restocked = False
    if pr.part_id:
        sp = (await db.execute(select(SparePart).where(SparePart.id == pr.part_id))).scalar_one_or_none()
        if sp is not None and sp.qty_in_stock is not None:
            sp.qty_in_stock = (sp.qty_in_stock or 0) + (pr.qty_requested or 1)
            restocked = True
    await audit(db, user=current_user, action="PART_WARRANTY_REPLACED", table_name="part_requests",
                record_id=str(pr.id),
                new_value={"part": pr.part_name, "restocked": restocked,
                           "qty": pr.qty_requested or 1}, request=request)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Warranty+replacement+received"
                                + ("+and+restocked" if restocked else ""), status_code=302)


@router.post("/part-sourcing/{sr_id}/close")
async def close_sourcing(sr_id: str, request: Request,
                         source_deal_id: str = Form(...), qty_sourced: int = Form(...),
                         db: AsyncSession = Depends(get_db), current_user: User = Depends(sm_allowed)):
    sr = (await db.execute(
        select(PartSourcingRequest).where(PartSourcingRequest.id == _as_uuid(sr_id))
    )).scalar_one_or_none()
    if not sr:
        raise HTTPException(404, "Sourcing request not found")
    was_already_closed = sr.status == "closed"
    sr.status = "closed"
    sr.source_deal_id = source_deal_id
    sr.qty_sourced = max(0, qty_sourced)
    sr.closed_at = app_now()
    sr.closed_by = current_user.username

    # If this request was already verified on the Part Master "Sourcing
    # Requests" tab (verify_sourcing() below) before the deal was closed
    # here, its qty_sourced was still 0 at verify-time so no stock was
    # credited then. Credit it now that the real quantity is known — but
    # only the first time this deal is closed, so re-submitting an
    # already-closed deal doesn't double-credit stock.
    if sr.verified and not was_already_closed and sr.part_id and sr.qty_sourced:
        part = (await db.execute(select(SparePart).where(SparePart.id == sr.part_id))).scalar_one_or_none()
        if part:
            db.add(SparePartsLedger(
                part_id=part.id, entry_type="IN", qty=sr.qty_sourced,
                cost_per_unit=float(part.unit_price), total_cost=sr.qty_sourced * float(part.unit_price),
                reference_type="sourcing_verified", reference_id=str(sr.id),
                created_by=current_user.username,
                notes=f"Sourcing request {sr.id} closed after verify — {sr.qty_sourced}x {sr.part_name}",
            ))
            part.qty_in_stock += sr.qty_sourced

    await audit(db, user=current_user, action="SOURCING_CLOSED", table_name="part_sourcing_requests",
                record_id=str(sr.id), new_value={"source_deal_id": source_deal_id, "qty_sourced": qty_sourced},
                request=request)
    await db.commit()
    return RedirectResponse(url="/crm/?success=Sourcing+deal+closed", status_code=302)


@router.post("/part-sourcing/{sr_id}/verify")
async def verify_sourcing(sr_id: str, request: Request,
                          db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    """Spare Parts Manager verifies the sourced documents/quantity on the Part Master
    'Sourcing Requests' tab. Independent of the Sales Manager's Close Deal step."""
    sr = (await db.execute(
        select(PartSourcingRequest).where(PartSourcingRequest.id == _as_uuid(sr_id))
    )).scalar_one_or_none()
    if not sr:
        return JSONResponse({"ok": False, "error": "Sourcing request not found"}, status_code=404)
    if sr.status != "closed":
        return JSONResponse({"ok": False, "error": "Pending at Sourcing — deal must be closed before verifying"}, status_code=409)
    if sr.verified:
        return JSONResponse({"ok": False, "error": "Already verified"}, status_code=409)
    sr.verified = True
    sr.verified_at = app_now()
    sr.verified_by = current_user.username

    # Credit the sourced quantity into Part Master stock (In Stock column),
    # via a ledger entry so the computed-stock and qty_in_stock column stay
    # in sync — same pattern as every other stock-affecting action.
    if sr.part_id and sr.qty_sourced:
        part = (await db.execute(select(SparePart).where(SparePart.id == sr.part_id))).scalar_one_or_none()
        if part:
            db.add(SparePartsLedger(
                part_id=part.id, entry_type="IN", qty=sr.qty_sourced,
                cost_per_unit=float(part.unit_price), total_cost=sr.qty_sourced * float(part.unit_price),
                reference_type="sourcing_verified", reference_id=str(sr.id),
                created_by=current_user.username,
                notes=f"Sourcing request {sr.id} verified — {sr.qty_sourced}x {sr.part_name}",
            ))
            part.qty_in_stock += sr.qty_sourced

    # Revert the originating Part Request from "procure" back to "requested"
    # now that stock is available — Part Request tab's Status column then
    # shows "Available" and its Action column shows the Handover button
    # again (instead of "Sent for Sourcing").
    if sr.part_request_id:
        pr = (await db.execute(select(PartRequest).where(PartRequest.id == sr.part_request_id))).scalar_one_or_none()
        if pr and pr.status == "procure":
            pr.status = "requested"

    await audit(db, user=current_user, action="SOURCING_VERIFIED", table_name="part_sourcing_requests",
                record_id=str(sr.id), new_value={"verified_by": current_user.username, "qty_sourced": sr.qty_sourced},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/part-sourcing/{sr_id}/confirm")
async def confirm_production_sourcing(sr_id: str, request: Request,
                                      db: AsyncSession = Depends(get_db),
                                      current_user: User = Depends(spm_allowed)):
    """Confirm Request — the Spare Parts Manager acknowledges a whole-lot
    request raised from Part Estimation. Production-sourced rows have no CRM
    deal to close, so this is their equivalent of the Verify step."""
    sr = (await db.execute(
        select(PartSourcingRequest).where(PartSourcingRequest.id == _as_uuid(sr_id))
    )).scalar_one_or_none()
    if not sr:
        return JSONResponse({"ok": False, "error": "Sourcing request not found"}, status_code=404)
    if sr.source != "production":
        return JSONResponse({"ok": False, "error": "Only production requests can be confirmed here"}, status_code=409)
    if sr.confirmed:
        return JSONResponse({"ok": True})

    sr.confirmed = True
    sr.confirmed_at = app_now()
    sr.confirmed_by = current_user.username
    await audit(db, user=current_user, action="PRODUCTION_SOURCING_CONFIRMED",
                table_name="part_sourcing_requests", record_id=str(sr.id),
                new_value={"confirmed_by": current_user.username, "lot": sr.lot_number},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/part-sourcing/{sr_id}/request-reupload")
async def request_sourcing_reupload(sr_id: str, request: Request,
                                    db: AsyncSession = Depends(get_db), current_user: User = Depends(spm_allowed)):
    """Send-to-Reupload from the Verify Sourcing modal — asks the uploader to
    re-submit documents. Does not change verified/status; the Sourcing
    Requests table's Action column is intentionally unaffected by this."""
    sr = (await db.execute(
        select(PartSourcingRequest).where(PartSourcingRequest.id == _as_uuid(sr_id))
    )).scalar_one_or_none()
    if not sr:
        return JSONResponse({"ok": False, "error": "Sourcing request not found"}, status_code=404)
    sr.reupload_requested = True
    sr.reupload_requested_at = app_now()
    sr.reupload_requested_by = current_user.username
    await audit(db, user=current_user, action="SOURCING_REUPLOAD_REQUESTED", table_name="part_sourcing_requests",
                record_id=str(sr.id), new_value={"requested_by": current_user.username}, request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/spare-parts/requests/bulk-action")
async def bulk_request_action(
    request: Request,
    action: str = Form(...),
    ids: list[str] = Form(default=[]),
    part_row_ids: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(spm_allowed),
):
    """Handover or delete several part/faulty requests in one go.

    Handover reuses the single-request rule exactly — quantity always equals
    what was requested, never a posted value — so the bulk path cannot hand
    over a different amount than the per-row button would. Only requests still
    at 'requested' are handed over; anything already actioned is skipped and
    reported rather than silently re-stamped.

    Delete is a soft close (status 'cancelled'), not a row removal: these rows
    are referenced by consumption reporting and the device's part history.
    """
    if action not in ("handover", "delete"):
        return JSONResponse({"ok": False, "error": "Unknown action"}, status_code=400)

    uuids = []
    for raw in ids:
        try:
            uuids.append(_as_uuid(raw))
        except Exception:
            continue
    if not uuids:
        return JSONResponse({"ok": False, "error": "No valid request ids"}, status_code=400)

    rows = (await db.execute(
        select(PartRequest).where(PartRequest.id.in_(uuids))
    )).scalars().all()

    processed, skipped = 0, 0
    reasons: list = []
    # Handover modal sends one crate row per request, in the same order.
    row_for = {str(_as_uuid(i)): r for i, r in zip(ids, part_row_ids)} if part_row_ids else {}
    now = app_now()
    for pr in rows:
        if action == "handover":
            err = await _do_handover(db, pr, row_for.get(str(pr.id), ""), current_user, request)
            if err:
                skipped += 1
                reasons.append(err)
                continue
            processed += 1
            continue
        else:
            if pr.status in ("handed_over", "received"):
                skipped += 1          # already fulfilled — cancelling would lose the record
                continue
            pr.status = "cancelled"
        pr.actioned_at = now
        pr.actioned_by = current_user.username
        processed += 1

    await audit(db, user=current_user,
                action="PART_REQUEST_BULK_" + action.upper(),
                table_name="part_requests", record_id=None,
                new_value={"processed": processed, "skipped": skipped,
                           "ids": [str(u) for u in uuids]},
                request=request)
    await db.commit()
    return JSONResponse({
        "ok": True, "processed": processed, "skipped": skipped,
        "reason": "; ".join(sorted(set(reasons))) if reasons else ("already actioned" if skipped else None),
    })


@router.post("/devices/{barcode}/pna/clear-all")
async def clear_all_pna(
    barcode: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Device Detail "Remove All PNA" (2026-10-17): clears every active PNA
    mark on one tag in one go. Same clear-not-delete rule as unticking one
    (is_active=False + cleared_by/at), so the history stays."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode)
    )).scalar_one_or_none()
    if not device:
        return JSONResponse({"ok": False, "error": "Tag not found"}, status_code=404)
    rows = (await db.execute(
        select(DevicePNAPart).where(
            DevicePNAPart.device_id == device.id, DevicePNAPart.is_active.is_(True))
    )).scalars().all()
    if not rows:
        return JSONResponse({"ok": True, "cleared": 0, "parts": []})
    now = app_now()
    for r in rows:
        r.is_active = False
        r.cleared_by = current_user.username
        r.cleared_at = now
    parts = sorted(r.part_name for r in rows)
    await audit(db, user=current_user, action="PART_PNA_CLEARED_ALL",
                table_name="device_pna_parts", record_id=str(device.id),
                new_value={"barcode": device.barcode, "parts": parts},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "cleared": len(rows), "parts": parts})


@router.post("/devices/{barcode}/pna")
async def mark_part_pna(
    barcode: str,
    request: Request,
    part_name: str = Form(...),
    part_category: str = Form(""),
    part_id: str = Form(""),
    marked: str = Form("1"),
    source: str = Form("parts_consumption"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark / unmark a part as PNA (Part Not Available) for one tag.

    Open to any signed-in user for the same reason IQC entry is: whoever is
    physically holding the unit needs to be able to record that a part cannot
    be sourced, and gating it behind the parts-manager role left engineers
    with no way to flag it.

    Unmarking clears the row rather than deleting it — that a part was once
    PNA is history a supervisor may need to explain a delay.
    """
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode)
    )).scalar_one_or_none()
    if not device:
        return JSONResponse({"ok": False, "error": "Tag not found"}, status_code=404)

    name = (part_name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Part name is required"}, status_code=400)

    row = (await db.execute(
        select(DevicePNAPart).where(
            DevicePNAPart.device_id == device.id,
            DevicePNAPart.part_name == name,
        )
    )).scalar_one_or_none()

    is_marked = str(marked).strip() in ("1", "true", "True", "yes", "on")
    now = app_now()

    if is_marked:
        if row is None:
            row = DevicePNAPart(
                device_id=device.id, barcode=device.barcode, part_name=name,
                part_category=(part_category or "").strip() or None,
                part_id=_as_uuid(part_id) if (part_id or "").strip() else None,
                source=(source or "parts_consumption").strip(),
            )
            db.add(row)
        # Re-marking a previously cleared part reopens the same row, so the
        # unique (device, part) pair stays one row rather than accumulating.
        row.is_active = True
        row.marked_by = current_user.username
        row.marked_at = now
        row.cleared_by = None
        row.cleared_at = None
    else:
        if row is None:
            return JSONResponse({"ok": True, "marked": False})
        row.is_active = False
        row.cleared_by = current_user.username
        row.cleared_at = now

    await audit(db, user=current_user,
                action="PART_PNA_MARKED" if is_marked else "PART_PNA_CLEARED",
                table_name="device_pna_parts", record_id=str(device.id),
                new_value={"barcode": device.barcode, "part": name, "source": source},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "marked": is_marked})
