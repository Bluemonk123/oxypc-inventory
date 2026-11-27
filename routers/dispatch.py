"""
Ready to Dispatch (#20) + telecaller dispatch requests (#21).

 - Telecaller raises a dispatch request from the Ready to Sale page.
 - Sales Manager approves it on the Ready to Dispatch page.
 - Approval enables the Sell button back on Ready to Sale.
"""
import csv
import io
import uuid
from utils.timezone import app_now
from utils.csv_decode import decode_csv_bytes
from fastapi import APIRouter, Depends, Form, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from templates_config import templates
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement
from models.lot import Lot
from models.sales import Sale, Return as SaleReturn
from models.dispatch_request import TelecallerDispatchRequest
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm
from services.audit_engine import audit
from utils.warranty import warranty_from_sold_at, latest_sold_at_map

router = APIRouter(tags=["dispatch"], dependencies=[Depends(verify_csrf)])

view_allowed = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.sales,
                             UserRole.sales_manager, UserRole.telecaller)
request_allowed = require_roles(UserRole.admin, UserRole.sales, UserRole.sales_manager, UserRole.telecaller)
# Approve / Reject / Bulk Approve on Inventory Requests are open to every
# signed-in user rather than admin + sales_manager only. Access is still
# governed by the permission matrix: has_perm() defaults to ALLOW when a role
# has no explicit row, so this is "everyone by default, restrict per role from
# Admin > Permission Matrix" rather than an unconditional grant.
approve_allowed = get_current_user
approve_perm = require_module_perm("inventory_requests", "edit")


def _as_uuid(v):
    try:
        return uuid.UUID(v)
    except Exception:
        return None


async def _build_requests_context(db: AsyncSession) -> dict:
    """Shared by /dispatch (TRC Dashboard) and /inventory-requests — both need
    the same Telecaller Requests + Sales Request for Lot data; the latter is
    the standalone page these two tables were moved to."""
    dr = (await db.execute(
        select(TelecallerDispatchRequest).order_by(TelecallerDispatchRequest.created_at.desc())
    )).scalars().all()
    device_requests = [r for r in dr if r.source != "lot"]
    lot_requests = [r for r in dr if r.source == "lot"]

    _seen = {}
    for r in dr:
        uname = r.telecaller_username or ""
        if uname and uname not in _seen:
            _seen[uname] = r.telecaller_name or uname
    telecaller_options = sorted(_seen.items(), key=lambda kv: kv[1].lower())

    return {"requests": device_requests, "lot_requests": lot_requests,
            "telecaller_options": telecaller_options}


async def _build_lot_overview(db: AsyncSession) -> list:
    """"Requests for Lot" table (item 28): every Lot with its device
    count, requested count (source='lot' requests raised against any device
    in that lot), a representative barcode for the Request button, and stock
    / selling price straight off the Lot row."""
    lots = (await db.execute(select(Lot).order_by(Lot.purchase_date.desc()))).scalars().all()
    if not lots:
        return []
    lot_ids = [l.id for l in lots]

    # Every per-lot figure this table shows is an aggregate, so Postgres computes
    # them. Building them in Python meant loading id, barcode and device_type for
    # every active device — ~22,000 rows — to produce roughly 280 table rows.
    stats = {}
    for lot_id, count, sample_barcode, device_type in (await db.execute(
        select(Device.lot_id, func.count(Device.id), func.min(Device.barcode),
               # Most common device type in the lot, matching the old
               # max(type_counts) — and deterministic on ties, which the Python
               # version was not (it kept whichever type it happened to see first).
               func.mode().within_group(Device.device_type))
        .where(Device.lot_id.in_(lot_ids), Device.is_active == True, Device.is_trashed == False)
        .group_by(Device.lot_id)
    )).all():
        stats[str(lot_id)] = {"count": count, "barcode": sample_barcode,
                              "device_type": device_type}

    # Summed straight to the lot rather than per device and re-summed in Python —
    # the total is the same either way.
    requested_by_lot = {str(lot_id): int(qty or 0) for lot_id, qty in (await db.execute(
        select(Device.lot_id, func.sum(TelecallerDispatchRequest.qty_requested))
        .select_from(TelecallerDispatchRequest)
        .join(Device, TelecallerDispatchRequest.device_id == Device.id)
        .where(TelecallerDispatchRequest.source == "lot",
               Device.lot_id.in_(lot_ids), Device.is_active == True, Device.is_trashed == False)
        .group_by(Device.lot_id)
    )).all()}

    overview = []
    for lot in lots:
        s = stats.get(str(lot.id))
        if not s:
            continue  # no point showing a lot with nothing to request
        overview.append({
            "lot_number": lot.lot_number,
            "purchase_date": lot.purchase_date,
            "supplier_name": lot.supplier_name,
            "device_type": s["device_type"] or "—",
            "total_quantity": s["count"],
            "requested_quantity": requested_by_lot.get(str(lot.id), 0),
            "stock_price": float(lot.buying_price or 0),
            "selling_price": float(lot.selling_price) if lot.selling_price is not None else None,
            "sample_barcode": s["barcode"],
        })
    return overview


@router.get("/inventory-requests", response_class=HTMLResponse)
async def inventory_requests(request: Request, db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(view_allowed)):
    ctx = await _build_requests_context(db)
    return templates.TemplateResponse("dispatch/inventory_requests.html", {
        "request": request, "current_user": current_user, **ctx,
    })


@router.post("/inventory-requests/upload-tags")
async def upload_request_tags(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(approve_allowed),
):
    """Upload Tags on Inventory Requests — reads a single-column file of tag
    numbers and reports which ones have a PENDING telecaller request, so the
    page can tick those rows for bulk approve.

    Read-only: approves nothing. The caller still has to press Approve
    Selected, so an upload can never approve anything on its own.

    Tags are decoded with decode_csv_bytes because these files come out of
    Excel and are frequently cp1252, not UTF-8.
    """
    content = await file.read()
    reader = csv.DictReader(io.StringIO(decode_csv_bytes(content)))

    # Read by header name so extra columns can't shift the one we want.
    field_map = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    key = field_map.get("tag_number") or field_map.get("barcode")
    if not key:
        return JSONResponse(
            {"error": "File must have a 'tag_number' (or 'barcode') column header"},
            status_code=400,
        )

    tags, errors, seen = [], [], set()
    for i, row in enumerate(reader, start=2):
        tag = (row.get(key) or "").strip()
        if not tag:
            errors.append(f"Row {i}: tag_number is empty")
            continue
        if tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)

    empty = {"found": [], "no_pending": [], "not_requested": [],
             "not_found": [], "errors": errors}
    if not tags:
        return JSONResponse(empty)

    # Two set-based queries, never one per tag — the database is a
    # transcontinental round trip away.
    req_rows = (await db.execute(
        select(TelecallerDispatchRequest.barcode, TelecallerDispatchRequest.status)
        .where(TelecallerDispatchRequest.barcode.in_(tags),
               TelecallerDispatchRequest.source != "lot")
    )).all()
    statuses: dict = {}
    for barcode, status in req_rows:
        statuses.setdefault(barcode, set()).add(status)

    known_devices = set((await db.execute(
        select(Device.barcode).where(Device.barcode.in_(tags))
    )).scalars().all())

    found, no_pending, not_requested, not_found = [], [], [], []
    for tag in tags:
        if tag in statuses:
            (found if "requested" in statuses[tag] else no_pending).append(tag)
        elif tag in known_devices:
            not_requested.append(tag)
        else:
            not_found.append(tag)

    return JSONResponse({"found": found, "no_pending": no_pending,
                         "not_requested": not_requested,
                         "not_found": not_found, "errors": errors})


@router.get("/dispatch", response_class=HTMLResponse)
async def dispatch_list(request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(view_allowed)):
    rows = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where(Device.current_stage == DeviceStage.ready_to_sale, Device.is_active == True,
               Device.is_trashed == False)
        .order_by(Device.updated_at.desc())
    )).all()
    device_ids = [d.id for d, _ in rows]

    dr = (await db.execute(
        select(TelecallerDispatchRequest).order_by(TelecallerDispatchRequest.created_at.desc())
    )).scalars().all()
    approved_qty = {}
    for r in dr:
        if r.status == "approved":
            approved_qty[str(r.device_id)] = approved_qty.get(str(r.device_id), 0) + r.qty_requested

    # Lot-level requests (raised from Ready to Sale's Model Summary) get their
    # own "Sales Request for Lot" table, separate from per-device requests.
    device_requests = [r for r in dr if r.source != "lot"]
    lot_requests = [r for r in dr if r.source == "lot"]

    sold_by = {}
    if device_ids:
        sold_rows = (await db.execute(
            select(Sale.device_id, func.count(Sale.id))
            .where(Sale.device_id.in_(device_ids)).group_by(Sale.device_id)
        )).all()
        for did, cnt in sold_rows:
            sold_by[str(did)] = cnt

    # ── Timeline (item 10): days since device entered ready_to_sale stage ─────
    ready_at_map = {}
    if device_ids:
        ready_rows = (await db.execute(
            select(StageMovement.device_id, func.max(StageMovement.moved_at))
            .where(StageMovement.device_id.in_(device_ids),
                   StageMovement.to_stage == DeviceStage.ready_to_sale)
            .group_by(StageMovement.device_id)
        )).all()
        for did, moved_at in ready_rows:
            ready_at_map[str(did)] = moved_at
    _now = app_now()

    # ── Warranty (2026-11-27): only the warranty picked on the latest sale ───
    from utils.warranty import latest_sale_map, effective_warranty_for_sale
    warranty_map = {}
    for did, _sale in (await latest_sale_map(db, device_ids)).items():
        w = effective_warranty_for_sale(_sale)
        if w:
            warranty_map[did] = w

    total_ready = len(rows)
    ready_d15 = ready_d30 = ready_d45 = ready_d60 = ready_d60plus = 0
    buckets = {"A": [], "B": [], "C": [], "D": []}
    for device, lot_number in rows:
        gv = device.grade.value if device.grade else None
        if gv == "A":
            key = "A"
        elif gv == "B":
            key = "B"
        elif gv == "D":
            key = "D"
        else:
            key = "C"  # C, scrap, ungraded → C
        qty = device.qty or 1
        disp = approved_qty.get(str(device.id), 0)
        _ready_at = ready_at_map.get(str(device.id))
        _timeline = (_now - _ready_at).days if _ready_at else None
        if _timeline is not None:
            if _timeline <= 15:
                ready_d15 += 1
            elif _timeline <= 30:
                ready_d30 += 1
            elif _timeline <= 45:
                ready_d45 += 1
            elif _timeline <= 60:
                ready_d60 += 1
            else:
                ready_d60plus += 1
        _w = warranty_map.get(str(device.id))
        buckets[key].append({
            "barcode": device.barcode, "model": device.model or device.brand or "—",
            "lot": lot_number, "qty": qty, "dispatched": disp,
            "pending": max(0, qty - disp), "sold": sold_by.get(str(device.id), 0),
            "timeline": _timeline,
            "warranty": _w["label"] if _w else None,
            "warranty_status": _w["status"] if _w else None,
        })

    # Per-grade summary for the count cards (total + dispatched/pending/sold split)
    card_stats = {}
    for g, items in buckets.items():
        card_stats[g] = {
            "total": len(items),
            "dispatched": sum(i["dispatched"] for i in items),
            "pending": sum(i["pending"] for i in items),
            "sold": sum(i["sold"] for i in items),
        }

    # Ready-to-Sale age summary card: total + split by days since ready_to_sale
    ready_summary = {
        "total": total_ready,
        "d15": ready_d15,        # ≤15 days
        "d30": ready_d30,        # 16–30 days
        "d45": ready_d45,        # 31–45 days
        "d60": ready_d60,        # 46–60 days
        "d60plus": ready_d60plus,  # > 60 days
    }

    # Distinct telecallers for the request-section filter dropdown
    _seen = {}
    for r in dr:
        uname = r.telecaller_username or ""
        if uname and uname not in _seen:
            _seen[uname] = r.telecaller_name or uname
    telecaller_options = sorted(_seen.items(), key=lambda kv: kv[1].lower())

    # ── Returned Devices: split by warranty status ───────────────────────────
    ret_rows = (await db.execute(
        select(Device, Sale, SaleReturn)
        .join(SaleReturn, SaleReturn.device_id == Device.id, isouter=True)
        .join(Sale, SaleReturn.sale_id == Sale.id, isouter=True)
        .where(Device.return_status == True, Device.is_active == True, Device.is_trashed == False)
        .order_by(SaleReturn.return_date.desc())
    )).all()

    returned_within_warranty = []
    returned_out_of_warranty = []
    for device, sale, ret in ret_rows:
        # In warranty when returned (2026-11-27): the status saved on the
        # return (picked warranty at return time), else worked out now.
        _ws = getattr(ret, "warranty_status", None) if ret else None
        if _ws in ("in_warranty", "out_of_warranty", "no_warranty"):
            _w = {"status": "active"} if _ws == "in_warranty" else None
        else:
            from utils.warranty import effective_warranty_for_sale
            _w = effective_warranty_for_sale(sale)
        entry = {
            "barcode": device.barcode,
            "brand": device.brand or "",
            "model": device.model or "",
            "return_date": ret.return_date if ret else None,
            "customer_name": (sale.customer_name if sale else None) or "—",
            "sold_by": (sale.sold_by if sale else None) or "—",
            "reason": (ret.reason if ret else None) or "—",
        }
        if _w and _w["status"] == "active":
            returned_within_warranty.append(entry)
        else:
            returned_out_of_warranty.append(entry)

    return templates.TemplateResponse("dispatch/list.html", {
        "request": request, "current_user": current_user,
        "buckets": buckets, "card_stats": card_stats, "requests": device_requests,
        "lot_requests": lot_requests,
        "telecaller_options": telecaller_options,
        "ready_summary": ready_summary,
        "returned_within_warranty": returned_within_warranty,
        "returned_out_of_warranty": returned_out_of_warranty,
    })


@router.post("/dispatch/request")
async def create_dispatch_request(request: Request, barcode: list[str] = Form(...), qty: int = Form(1),
                                  source: str = Form("device"), model_name: str = Form(None),
                                  model_make: str = Form(None),
                                  db: AsyncSession = Depends(get_db),
                                  current_user: User = Depends(request_allowed)):
    barcodes = [b.strip() for b in barcode if b and b.strip()]
    if not barcodes:
        raise HTTPException(404, "Device not found")
    _source = "lot" if source == "lot" else "device"
    raised, not_found = [], []
    for bc in barcodes:
        device = (await db.execute(select(Device).where(Device.barcode == bc))).scalar_one_or_none()
        if not device:
            not_found.append(bc)
            continue
        db.add(TelecallerDispatchRequest(
            device_id=device.id, barcode=device.barcode,
            telecaller_username=current_user.username, telecaller_name=current_user.full_name,
            qty_requested=max(1, qty), qty_available=device.qty or 1,
            grade=device.grade.value if device.grade else None, status="requested",
            source=_source, model_name=(model_name or None), model_make=(model_make or None),
        ))
        raised.append(bc)
    if not raised:
        return RedirectResponse(url="/sales/ready?error=No+valid+devices+found", status_code=302)
    await audit(db, user=current_user, action="DISPATCH_REQUESTED", table_name="telecaller_dispatch_requests",
                record_id=None, new_value={"barcodes": raised, "qty": qty}, request=request)
    await db.commit()
    msg = f"Dispatch+request+raised+for+{len(raised)}+device(s)"
    if not_found:
        msg += f"+({len(not_found)}+not+found)"
    return RedirectResponse(url=f"/sales/ready?success={msg}", status_code=302)


def _apply_approval(r: TelecallerDispatchRequest, current_user: User) -> bool:
    """Single source of truth for what "approved" means to a dispatch request.
    Both the single-row and the bulk endpoint go through here so the two can
    never drift apart. Returns False when the row is not eligible (already
    approved, or rejected) so callers can skip it instead of failing."""
    if r.status != "requested":
        return False
    r.status = "approved"
    r.approved_at = app_now()
    r.approved_by = current_user.username
    return True


@router.post("/dispatch/{req_id}/approve")
async def approve_dispatch(req_id: str, request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(approve_allowed),
                           _perm: User = Depends(approve_perm)):
    r = (await db.execute(
        select(TelecallerDispatchRequest).where(TelecallerDispatchRequest.id == _as_uuid(req_id))
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Request not found")
    if not _apply_approval(r, current_user):
        return RedirectResponse(url="/inventory-requests?error=Request+is+no+longer+pending",
                                status_code=302)
    await audit(db, user=current_user, action="DISPATCH_APPROVED", table_name="telecaller_dispatch_requests",
                record_id=str(r.id), new_value={"barcode": r.barcode}, request=request)
    await db.commit()
    return RedirectResponse(url="/dispatch?success=Request+approved", status_code=302)


@router.post("/dispatch/bulk-approve")
async def bulk_approve_dispatch(request: Request, ids: list[str] = Form([]),
                                db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(approve_allowed),
                                _perm: User = Depends(approve_perm)):
    """Approve every ticked row from one of the Inventory Requests tables.

    Deliberately set-based: one IN query for the whole batch. The database
    lives in a different region from the app, so a per-id SELECT loop turns a
    50-row approval into 50 cross-region round trips."""
    uuids = [u for u in (_as_uuid(i) for i in ids if i) if u is not None]
    if not uuids:
        return RedirectResponse(url="/inventory-requests?error=No+requests+selected", status_code=302)

    rows = (await db.execute(
        select(TelecallerDispatchRequest).where(TelecallerDispatchRequest.id.in_(uuids))
    )).scalars().all()

    approved = []
    for r in rows:
        if _apply_approval(r, current_user):
            approved.append(r)
    # Anything asked for but not approved — ineligible status, or an id that no
    # longer exists — is reported back as skipped rather than aborting the batch.
    skipped = len(uuids) - len(approved)

    if approved:
        await audit(db, user=current_user, action="DISPATCH_BULK_APPROVED",
                    table_name="telecaller_dispatch_requests", record_id=f"bulk:{len(approved)}",
                    new_value={"request_ids": [str(r.id) for r in approved],
                               "barcodes": [r.barcode for r in approved],
                               "skipped": skipped},
                    request=request)
        await db.commit()
    return RedirectResponse(
        url=f"/inventory-requests?success={len(approved)}+approved,+{skipped}+skipped",
        status_code=302)


@router.post("/dispatch/{req_id}/reject")
async def reject_dispatch(req_id: str, request: Request, notes: str = Form(...),
                          db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(approve_allowed),
                          _perm: User = Depends(approve_perm)):
    r = (await db.execute(
        select(TelecallerDispatchRequest).where(TelecallerDispatchRequest.id == _as_uuid(req_id))
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Request not found")
    r.status = "rejected"
    r.rejected_notes = notes.strip()
    r.rejected_at = app_now()
    r.rejected_by = current_user.username
    await audit(db, user=current_user, action="DISPATCH_REJECTED", table_name="telecaller_dispatch_requests",
                record_id=str(r.id), new_value={"barcode": r.barcode, "notes": r.rejected_notes}, request=request)
    await db.commit()
    return RedirectResponse(url="/inventory-requests?success=Request+rejected", status_code=302)
