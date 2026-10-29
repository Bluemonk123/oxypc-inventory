from templates_config import templates
import logging
import uuid
from services.parts_changed import parts_badge_map
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_, update, text
from datetime import datetime as _dt
from utils.timezone import app_now
from utils.master_data import master_values, entity_values
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, DeviceGrade, StageMovement, STAGE_LABELS, DROPDOWN_STAGES, COSMETIC_STAGES, reset_repair_cycle_on_entry
from models.lot import Lot, LotLineItem
from models.master import EXTERNAL_PARTNER_TEST_ENTITY, EXTERNAL_PARTNER_TEST_LOT_PREFIX
from models.crm import CRMSourcingDeal
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm, require_additional_perm
from services.audit_engine import audit
from utils.fk_purge import purge_references
from services.event_bus import EventType, publish
from models.sales import Sale, Return
from models.spare_parts import SparePartConsumption
from models.engines import RepairAttempt
from models.qc import QCCheck
from models.iqc_inspection import IQCInspection
from models.cost_config import CostConfig
from models.stock_transfer import StockTransfer
from models.stock_validation import StockValidation
from models.part_request import PartRequest
from models.spare_parts import SparePart
from models.location import StorageLocation, ZoneType, ZONE_LABELS, UnitType, UNIT_TYPE_LABELS
from models.work_order import WorkOrder
from models.bucket import Bucket, _new_bucket_number
from models.pna_part import DevicePNAPart
from routers.transfers import _gen_work_id
from services.notifications import create_notification
from services.work_order_close import close_open_work_orders, reassign_open_work_orders, reuse_open_stress_work_order, wo_open
from services.location_defaults import ensure_stage_location
from utils.flag import flag_badge_html, is_flagged

_log = logging.getLogger(__name__)

router = APIRouter(tags=["stock"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager,
                         UserRole.qc_inspector, UserRole.sales_manager)


async def build_cost_parts_map(db: AsyncSession, device_ids: list):
    """Batch-fetch parts-required / parts-repaired / repair-cost per device_id
    for the Stock Inward + TRC Production 'Cost & Parts' table (Batch D).
    Returns {device_id: {parts_required, parts_repaired, repair_cost}}.
    Judgment call: 'Sale Price' is NOT a Device-level field in this schema —
    Sale.sale_price is per-sale-transaction. Since these devices have not been
    sold yet (Stock Inward / TRC Production stages), we surface device_price
    (Stock Price) as Stock Cost and leave Sale Price blank/em-dash until a
    canonical "intended sale price" field is added to Device. This is flagged
    in the report; no schema field was invented to avoid violating the
    schema-review-board gate.
    """
    out = {}
    if not device_ids:
        return out

    pr_rows = (await db.execute(
        select(PartRequest.device_id, PartRequest.status, PartRequest.qty_handed_over,
               PartRequest.part_id)
        .where(PartRequest.device_id.in_(device_ids))
    )).all()

    part_ids = {r.part_id for r in pr_rows if r.part_id}
    price_map = {}
    if part_ids:
        sp_rows = (await db.execute(
            select(SparePart.id, SparePart.unit_price).where(SparePart.id.in_(part_ids))
        )).all()
        price_map = {r.id: r.unit_price for r in sp_rows}

    for r in pr_rows:
        d = out.setdefault(r.device_id, {"parts_required": 0, "parts_repaired": 0, "repair_cost": 0})
        d["parts_required"] += 1
        if r.status == "handed_over":
            d["parts_repaired"] += 1
            unit_price = price_map.get(r.part_id) or 0
            d["repair_cost"] += float(unit_price) * (r.qty_handed_over or 0)
    return out


@router.get("/lots/api/exists")
async def lot_number_exists(lot_number: str, exclude_id: str = "",
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    """Live duplicate check for Lot Number (item 13). Returns {"exists": bool}.
    exclude_id lets the Edit Lot page ignore the lot being edited."""
    ln = (lot_number or "").strip()
    if not ln:
        return JSONResponse({"exists": False})
    q = select(Lot.id).where(Lot.lot_number == ln)
    rows = (await db.execute(q)).all()
    exists = False
    for (rid,) in rows:
        if exclude_id and str(rid) == str(exclude_id):
            continue
        exists = True
        break
    return JSONResponse({"exists": exists})

# "L3 Engineer" retired along with the /repair/l3 page — L3 work now runs on L3L4- WorkOrders.
# Department here only labels the StockTransfer row (it never moves a device stage), and no
# transfer has ever used it, so dropping it changes no history.
STOCK_DEPARTMENTS = [
    "IQC Handler", "L1 Engineer", "L2 Engineer",
    "QC Handler", "Inventory Manager", "Sales Manager", "Parts Manager",
]

import csv
import io
from fastapi.responses import StreamingResponse


async def _next_lot_number(db: AsyncSession) -> str:
    """
    Auto-generate the next LOT-NNN number.
    Uses MAX on the numeric suffix (not COUNT) so gaps from trashed lots
    don't cause collisions: e.g., if LOT-003 was trashed, next is still
    LOT-004 and we never accidentally suggest LOT-003 again.
    Falls back to LOT-001 when the table is empty.
    """
    from sqlalchemy import text as _text
    result = await db.execute(
        _text(
            "SELECT COALESCE(MAX(CAST(REGEXP_REPLACE(lot_number, '[^0-9]', '', 'g') AS INTEGER)), 0)"
            " FROM lots WHERE lot_number ~ '^LOT-[0-9]'"
        )
    )
    max_num = result.scalar() or 0
    return f"LOT-{max_num + 1:03d}"


@router.get("/lots", response_class=HTMLResponse)
async def list_lots(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    q: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
):
    # Build filtered base statement — exclude trashed lots
    base_stmt = select(Lot).where(Lot.is_trashed.isnot(True))
    if q:
        _like = f"%{q}%"
        base_stmt = base_stmt.where(or_(
            Lot.supplier_name.ilike(_like),
            Lot.lot_number.ilike(_like),
            Lot.vendor_name.ilike(_like),
        ))
    else:
        # No explicit search -> exclude External Partner test Lots from the
        # default view, same convention as the Device.entity exclusion
        # elsewhere in this file. An explicit search for "EPT-..." still
        # finds them (Lot.lot_number is a required, unique column, so this
        # is NULL-safe with no extra handling needed).
        base_stmt = base_stmt.where(~Lot.lot_number.ilike(f"{EXTERNAL_PARTNER_TEST_LOT_PREFIX}%"))
    if date_from:
        try:
            base_stmt = base_stmt.where(Lot.purchase_date >= _dt.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            base_stmt = base_stmt.where(Lot.purchase_date <= _dt.strptime(date_to, "%Y-%m-%d"))
        except ValueError:
            pass

    # Fetch all matching lots — one query (client-side DataTables pagination)
    lots_result = await db.execute(
        base_stmt.order_by(Lot.created_at.desc())
    )
    lots = lots_result.scalars().all()
    total = len(lots)

    from utils.lot_helpers import build_lot_stats
    lot_stats = await build_lot_stats(db, lots)
    return templates.TemplateResponse("lots/list.html", {
        "request": request, "lot_stats": lot_stats, "current_user": current_user,
        "total": total,
        "q": q, "date_from": date_from, "date_to": date_to,
    })


@router.get("/lots/export-all-csv")
async def export_all_lots_csv(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    q: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
):
    """Export filtered lots as CSV — respects the same q / date_from / date_to
    filters that are active on the Lot Management list page."""

    # ── Apply the same filter logic as list_lots ─────────────────────────────
    base_stmt = select(Lot).where(Lot.is_trashed.isnot(True))
    if q:
        base_stmt = base_stmt.where(
            or_(Lot.lot_number.ilike(f"%{q}%"), Lot.supplier_name.ilike(f"%{q}%"))
        )
    if date_from:
        try:
            base_stmt = base_stmt.where(Lot.purchase_date >= _dt.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            base_stmt = base_stmt.where(Lot.purchase_date <= _dt.strptime(date_to, "%Y-%m-%d"))
        except ValueError:
            pass

    result = await db.execute(base_stmt.order_by(Lot.created_at.desc()))
    lots = result.scalars().all()

    # ── Per-lot device + sold counts ─────────────────────────────────────────
    lot_ids = [lot.id for lot in lots]
    dev_map: dict = {}
    sold_map: dict = {}
    if lot_ids:
        dev_rows = await db.execute(
            select(Device.lot_id, func.count(Device.id))
            .where(Device.lot_id.in_(lot_ids), Device.is_active == True)
            .group_by(Device.lot_id)
        )
        dev_map = dict(dev_rows.fetchall())

        sold_rows = await db.execute(
            select(Device.lot_id, func.count(Device.id))
            .where(Device.lot_id.in_(lot_ids), Device.current_stage == DeviceStage.sold)
            .group_by(Device.lot_id)
        )
        sold_map = dict(sold_rows.fetchall())

    # ── Build CSV with full column set matching the UI table ─────────────────
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Lot #", "GRN #", "Supplier", "Created At",
        "Purchase Date", "Rcvd Date (GRN)", "Invoice No.", "Invoice Value",
        "SGST", "CGST", "IGST", "Qty (PO)",
        "Registered Devices", "Sold", "Buying Price (₹)",
    ])
    for lot in lots:
        grn = lot.grn_system_number or (str(lot.grn_number_new) if lot.grn_number_new else "")
        writer.writerow([
            lot.lot_number,
            grn,
            lot.supplier_name or "",
            lot.created_at.strftime("%d-%m-%Y %H:%M") if lot.created_at else "",
            lot.purchase_date.strftime("%d-%m-%Y") if lot.purchase_date else "",
            lot.grn_date.strftime("%d-%m-%Y") if lot.grn_date else "",
            lot.invoice_no or "",
            float(lot.invoice_value or 0) or "",
            float(lot.sgst or 0) or "",
            float(lot.cgst or 0) or "",
            float(lot.igst or 0) or "",
            lot.qty or 0,
            dev_map.get(lot.id, 0),
            sold_map.get(lot.id, 0),
            float(lot.buying_price or 0),
        ])

    from datetime import datetime as _dtnow
    suffix = f"-{q}" if q else ""
    filename = f"lots-export{suffix}-{_dtnow.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/lots/export-csv")
async def export_selected_lots_csv(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Export devices belonging to selected lot IDs as CSV."""
    form = await request.form()
    lot_ids_raw = form.getlist("lot_ids")
    if not lot_ids_raw:
        return RedirectResponse(url="/lots?warning=No+lots+selected", status_code=302)

    rows = await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.lot_id.in_(lot_ids_raw), Device.is_active == True)
        .order_by(Lot.lot_number, Device.barcode)
    )
    devices = rows.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "barcode", "brand", "model", "lot_number", "grade",
        "ram_gb", "storage_gb", "storage_type", "current_stage", "serial_no",
    ])
    for device, lot_number in devices:
        writer.writerow([
            device.barcode,
            device.brand or "",
            device.model or "",
            lot_number,
            device.grade or "",
            device.ram_gb or "",
            device.storage_gb or "",
            device.storage_type or "",
            device.current_stage.value if device.current_stage else "",
            device.serial_no or "",
        ])
    from datetime import datetime as _dtnow
    filename = f"lot-devices-{_dtnow.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/lots/new", response_class=HTMLResponse)
async def new_lot_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    # Optional pre-fill params from a CRM sourcing deal
    from_deal: str = Query(default=None),
    prefill_supplier: str = Query(default=""),
    prefill_qty: str = Query(default=""),
    prefill_price: str = Query(default=""),
    prefill_notes: str = Query(default=""),
    prefill_po: str = Query(default=""),
    grn_system_number: str = Query(default=""),
    grn_date: str = Query(default=""),
    supplier: str = Query(default=""),
    lot_number: str = Query(default=""),
):
    next_num = await _next_lot_number(db)
    prefill = {
        "from_deal": from_deal or "",
        "supplier": prefill_supplier,
        "qty": prefill_qty,
        "price": prefill_price,
        "notes": prefill_notes,
        "po": prefill_po,
    } if from_deal else None
    return templates.TemplateResponse("lots/form.html", {
        "request": request, "next_lot_number": next_num,
        "current_user": current_user, "error": None,
        "prefill": prefill, "lot": None,
        "prefill_grn_system": grn_system_number,
        "prefill_grn_date": _norm_grn_date(grn_date),
        "prefill_supplier_locked": supplier,
        "prefill_lot_number": lot_number,
        "from_grn": bool(grn_system_number or lot_number or supplier),
        "today_date": app_now().strftime('%Y-%m-%d'),
    })


def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d") if s else None
    except Exception:
        return None


def _parse_decimal(s: str):
    try:
        return float(s) if s else None
    except Exception:
        return None


def _norm_grn_date(s: str) -> str:
    """Normalise a free-text invoice date (the GRN 'Date of Invoice') to
    YYYY-MM-DD for the date picker. Returns '' if unparseable (the form then
    falls back to today)."""
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
                "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
                "%B %d, %Y", "%d-%b-%y", "%d/%m/%y"):
        try:
            return _dt.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return ""


@router.post("/lots/new")
async def create_lot(
    request: Request,
    background_tasks: BackgroundTasks,
    lot_number: str = Form(...),
    supplier_name: str = Form(...),
    buying_price: str = Form(default=""),
    # "Target Selling Price" on the lot form. Internal only — the partner
    # catalog never reads it; it quotes buying_price as the Base Price.
    selling_price: str = Form(""),
    qty: str = Form(default=""),
    purchase_date: str = Form(...),
    grn_system_number: str = Form(""),
    grn_number_new: str = Form(""),
    grn_date: str = Form(""),
    invoice_no: str = Form(""),
    invoice_value: str = Form(""),
    taxable_amount: str = Form(""),
    sgst: str = Form(""),
    cgst: str = Form(""),
    igst: str = Form(""),
    vehicle_number: str = Form(""),
    e_way_bill: str = Form(""),
    po_number: str = Form(""),
    notes: str = Form(""),
    crm_deal_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("lots", "add")),
    # The check above only looks at the "lots" module's "enable" bit (see
    # has_perm()'s docstring) — this is the real, enforced gate: Add Lot from
    # the Role Additional Permissions tab.
    _perm2: User = Depends(require_additional_perm("add_lot")),
):
    existing = await db.execute(select(Lot).where(Lot.lot_number == lot_number))
    if existing.scalar_one_or_none():
        next_num = await _next_lot_number(db)
        return templates.TemplateResponse("lots/form.html", {
            "request": request, "next_lot_number": next_num, "current_user": current_user,
            "error": "Lot number already exists", "lot": None,
        })
    # Quantity defaults to 1 (never 0) — several downstream calculations divide
    # by lot.qty (e.g. per-unit cost = buying_price / qty), so 0 would crash them.
    qty_val = int(qty) if qty.strip().isdigit() else 1
    lot = Lot(
        lot_number=lot_number,
        supplier_name=supplier_name,
        buying_price=float(buying_price) if buying_price.strip() else 0,
        qty=qty_val,
        purchase_date=datetime.strptime(purchase_date, "%Y-%m-%d"),
        grn_system_number=grn_system_number or None,
        grn_number_new=int(grn_number_new) if grn_number_new else None,
        grn_date=_parse_date(grn_date),
        # Invoice Date field was removed from the form - GRN/Invoice (the
        # renamed Purchase/GRN Date field) now doubles as the invoice date.
        invoice_date=datetime.strptime(purchase_date, "%Y-%m-%d"),
        invoice_no=invoice_no or None,
        invoice_value=_parse_decimal(invoice_value),
        selling_price=_parse_decimal(selling_price),
        taxable_amount=_parse_decimal(taxable_amount),
        sgst=_parse_decimal(sgst),
        cgst=_parse_decimal(cgst),
        igst=_parse_decimal(igst),
        vehicle_number=vehicle_number or None,
        e_way_bill=e_way_bill or None,
        po_number=po_number or None,
        notes=notes or None,
        created_by=current_user.username,
    )
    db.add(lot)
    await db.flush()   # get lot.id before audit/commit

    await audit(db, action="LOT_CREATED", user=current_user,
                table_name="lots", record_id=str(lot.id),
                new_value={"lot_number": lot_number, "supplier": supplier_name,
                           "qty": qty_val, "buying_price": buying_price},
                request=request)

    # ── CRM integration: back-link sourcing deal to this lot ──────────────
    if crm_deal_id and crm_deal_id.strip():
        deal_r = await db.execute(
            select(CRMSourcingDeal).where(CRMSourcingDeal.id == crm_deal_id.strip())
        )
        deal = deal_r.scalar_one_or_none()
        if deal:
            deal.linked_lot_id = lot.id
            deal.stage = "won"

    await db.commit()

    publish(EventType.LOT_CREATED, {
        "lot_id": str(lot.id),
        "lot_number": lot.lot_number,
        "supplier": supplier_name,
        "qty": qty_val,
        "buying_price": buying_price,
        "_source": "stock_html",
    }, background_tasks)

    if crm_deal_id and crm_deal_id.strip():
        return RedirectResponse(
            url=f"/crm/sourcing/{crm_deal_id.strip()}?success=Lot+{lot.lot_number}+created+and+linked",
            status_code=302,
        )
    return RedirectResponse(url="/lots?success=Lot+created", status_code=302)


@router.get("/lots/{lot_id}/edit", response_class=HTMLResponse)
async def edit_lot_form(lot_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed), error: str = Query(default="")):
    result = await db.execute(select(Lot).where(Lot.id == lot_id))
    lot = result.scalar_one_or_none()
    if not lot:
        raise HTTPException(404)
    return templates.TemplateResponse("lots/form.html", {
        # `error` arrives as a query param when a save was rejected (e.g. a
        # duplicate Lot Number) and we redirected back here.
        "request": request, "lot": lot, "next_lot_number": None,
        "current_user": current_user, "error": error or None,
    })


@router.post("/lots/{lot_id}/edit")
async def edit_lot(
    lot_id: str,
    request: Request,
    lot_number: str = Form(...),
    supplier_name: str = Form(...),
    buying_price: str = Form(default=""),
    # "Target Selling Price" on the lot form. Internal only — the partner
    # catalog never reads it; it quotes buying_price as the Base Price.
    selling_price: str = Form(""),
    qty: str = Form(default=""),
    purchase_date: str = Form(...),
    grn_system_number: str = Form(""),
    grn_number_new: str = Form(""),
    grn_date: str = Form(""),
    invoice_no: str = Form(""),
    invoice_value: str = Form(""),
    taxable_amount: str = Form(""),
    sgst: str = Form(""),
    cgst: str = Form(""),
    igst: str = Form(""),
    vehicle_number: str = Form(""),
    e_way_bill: str = Form(""),
    po_number: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_additional_perm("edit_lot")),
):
    result = await db.execute(select(Lot).where(Lot.id == lot_id))
    lot = result.scalar_one_or_none()
    if not lot:
        raise HTTPException(404)
    # Lot Number is editable on this page — reject a rename that collides with
    # another lot rather than letting the unique constraint surface as a 500.
    lot_number = (lot_number or "").strip()
    if lot_number != lot.lot_number:
        clash = (await db.execute(
            select(Lot.id).where(Lot.lot_number == lot_number, Lot.id != lot.id)
        )).scalar_one_or_none()
        if clash:
            from urllib.parse import quote
            return RedirectResponse(
                url=f"/lots/{lot_id}/edit?error={quote(f'Lot Number {lot_number} already exists')}",
                status_code=302)
    lot.lot_number = lot_number
    lot.supplier_name = supplier_name
    lot.buying_price = float(buying_price) if buying_price.strip() else 0
    lot.qty = int(qty) if qty.strip().isdigit() else 1
    lot.purchase_date = datetime.strptime(purchase_date, "%Y-%m-%d")
    lot.grn_system_number = grn_system_number or None
    lot.grn_number_new = int(grn_number_new) if grn_number_new else None
    lot.grn_date = _parse_date(grn_date)
    # Invoice Date field was removed from the form - GRN/Invoice (the renamed
    # Purchase/GRN Date field) now doubles as the invoice date.
    lot.invoice_date = datetime.strptime(purchase_date, "%Y-%m-%d")
    lot.invoice_no = invoice_no or None
    lot.invoice_value = _parse_decimal(invoice_value)
    lot.selling_price = _parse_decimal(selling_price)
    lot.taxable_amount = _parse_decimal(taxable_amount)
    lot.sgst = _parse_decimal(sgst)
    lot.cgst = _parse_decimal(cgst)
    lot.igst = _parse_decimal(igst)
    lot.vehicle_number = vehicle_number or None
    lot.e_way_bill = e_way_bill or None
    lot.po_number = po_number or None
    lot.notes = notes or None
    await db.commit()
    return RedirectResponse(url=f"/lots/{lot_id}?success=Lot+updated", status_code=302)


@router.post("/lots/{lot_id}/delete")
async def delete_lot_permanent(
    lot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """
    Permanently delete a lot, its devices and everything that references them.

    The cascade is derived from the database's own foreign keys at request
    time (utils/fk_purge), so tables added later are handled automatically.
    Records that merely mention the lot — supplier payments, CRM sourcing
    deals, telecalling records — are unlinked rather than deleted.
    """
    import uuid as _uuid
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

    lot_number = lot.lot_number
    lot_id_param = {"lot_id": str(uid)}

    # ── Clear everything that references this lot ────────────────────────────
    # Driven by the live FK catalog rather than a hand-written table list: the
    # old list had fallen 19 tables behind the schema, and because every FK
    # here is ON DELETE NO ACTION, one forgotten child row raised
    # ForeignKeyViolation and the page returned 500 (22 of 287 production lots
    # could not be deleted). See utils/fk_purge for the delete-vs-unlink rule.
    device_ids = (await db.execute(
        _text("SELECT id FROM devices WHERE lot_id = :lot_id"), lot_id_param)).scalars().all()
    purged = await purge_references(db, "devices", device_ids)
    if device_ids:
        await db.execute(_text("DELETE FROM devices WHERE lot_id = :lot_id"), lot_id_param)
        purged.append(f"deleted {len(device_ids)} devices")
    purged += await purge_references(db, "lots", [uid])

    # Audit before the row is gone
    await audit(
        db, action="LOT_DELETED", user=current_user,
        table_name="lots", record_id=str(uid),
        new_value={
            "lot_number": lot_number,
            "supplier": lot.supplier_name,
            "buying_price": str(lot.buying_price),
            "qty": lot.qty,
            "deleted_by": current_user.username,
            "cascade": purged,
        },
        request=request,
    )

    await db.delete(lot)
    try:
        await db.commit()
    except IntegrityError as e:
        # A reference the purge could not resolve. Roll back so the lot is left
        # intact and say which constraint blocked it, rather than 500ing.
        await db.rollback()
        _log.exception("lot delete blocked by a foreign key: %s", lot_number)
        detail = getattr(getattr(e, "orig", None), "constraint_name", None) or "a related record"
        return RedirectResponse(
            url=f"/lots?error={quote(lot_number + ' could not be deleted — still referenced by ' + str(detail))}",
            status_code=302,
        )

    return RedirectResponse(
        url=f"/lots?success={quote(lot_number + ' deleted permanently')}",
        status_code=302,
    )


@router.get("/lots/{lot_id}", response_class=HTMLResponse)
async def lot_detail(lot_id: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Lot).where(Lot.id == lot_id))
    lot = result.scalar_one_or_none()
    if not lot:
        raise HTTPException(404)
    devices_result = await db.execute(select(Device).where(Device.lot_id == lot_id, Device.is_active == True).order_by(Device.created_at))
    devices = devices_result.scalars().all()
    li_result = await db.execute(
        select(LotLineItem).where(LotLineItem.lot_id == lot_id).order_by(LotLineItem.sub_category, LotLineItem.brand, LotLineItem.model)
    )
    line_items = li_result.scalars().all()

    # ── Profit calculation (same COGS formula as dashboard lot_pl) ──────────

    revenue_result = await db.execute(
        select(func.coalesce(func.sum(Sale.sale_price), 0))
        .join(Device, Sale.device_id == Device.id)
        .where(Device.lot_id == lot.id)
    )
    lot_revenue = float(revenue_result.scalar() or 0)

    parts_result = await db.execute(
        select(func.coalesce(func.sum(SparePartConsumption.total_cost), 0))
        .where(SparePartConsumption.lot_id == lot.id)
    )
    parts_cost = float(parts_result.scalar() or 0)

    # Repair cost (2026-10-29, services/repair_cost.py) — same as the
    # Dashboard's Lot P&L: Labour of worked-on tags + Packing/Freight of sold
    # tags. lot_cosmetic_cost now carries Packing + Freight.
    from services.repair_cost import lot_repair_costs
    _lab, _pf, _ = (await lot_repair_costs(db, lot_ids=[lot.id])).get(lot.id, (0, 0, 0))
    labour_cost = float(_lab)
    cosmetic_cost = float(_pf)

    buying     = float(lot.buying_price or 0)
    total_cost = buying + parts_cost + labour_cost + cosmetic_cost
    lot_profit = lot_revenue - total_cost
    lot_margin = round((lot_profit / lot_revenue * 100) if lot_revenue > 0 else 0, 1)

    # ── Per-device sub-record counts for the devices table ──────────────────
    device_id_list = [d.id for d in devices]
    if device_id_list:
        # Repair attempts per device
        repair_q = await db.execute(
            select(RepairAttempt.device_id, func.count(RepairAttempt.id).label("cnt"))
            .where(RepairAttempt.device_id.in_(device_id_list))
            .group_by(RepairAttempt.device_id)
        )
        repair_map = {str(r.device_id): r.cnt for r in repair_q.all()}

        # QC checks per device
        qc_q = await db.execute(
            select(QCCheck.device_id, func.count(QCCheck.id).label("cnt"))
            .where(QCCheck.device_id.in_(device_id_list))
            .group_by(QCCheck.device_id)
        )
        qc_map = {str(r.device_id): r.cnt for r in qc_q.all()}

        # IQC done (distinct device_ids that have an IQC record)
        iqc_q = await db.execute(
            select(IQCInspection.device_id)
            .where(IQCInspection.device_id.in_(device_id_list))
            .distinct()
        )
        iqc_set = {str(r.device_id) for r in iqc_q.all()}

        # Sold status
        sold_q = await db.execute(
            select(Sale.device_id).where(Sale.device_id.in_(device_id_list))
        )
        sold_set = {str(r.device_id) for r in sold_q.all()}
    else:
        repair_map = {}
        qc_map     = {}
        iqc_set    = set()
        sold_set   = set()
    # ─────────────────────────────────────────────────────────────────────────

    return templates.TemplateResponse("lots/detail.html", {
        "request": request, "lot": lot, "devices": devices, "current_user": current_user,
        "line_items": line_items,
        "lot_revenue":       lot_revenue,
        "lot_buying":        buying,
        "lot_parts_cost":    parts_cost,
        "lot_labour_cost":   labour_cost,
        "lot_cosmetic_cost": cosmetic_cost,
        "lot_total_cost":    total_cost,
        "lot_profit":        lot_profit,
        "lot_margin":        lot_margin,
        # per-device sub-record counts
        "repair_map":  repair_map,
        "qc_map":      qc_map,
        "iqc_set":     iqc_set,
        "sold_set":    sold_set,
    })


def _stock_filters(device_type, lot_number, date_from, date_to, entity=""):
    from utils.date_filter import apply_date_range
    # Inventory Stock table is scoped strictly to devices currently AT the
    # Stock In stage — not "has ever had a GRN". A device only leaves this
    # list once it actually moves stage (Move to Production, scrap, etc.),
    # and only enters it once something (GRN mapping, Customise modal,
    # repair-complete, bucket assignment, ...) sets current_stage=stock_in.
    w = [Device.current_stage == DeviceStage.stock_in, Device.is_active == True,
         Device.is_trashed == False]
    if device_type:
        w.append(Device.device_type == device_type)
    if lot_number:
        w.append(Lot.lot_number == lot_number)
    if entity:
        w.append(Device.entity == entity)
    else:
        # No explicit Entity filter -> exclude the External Partner
        # test-data entity by default, same convention as
        # routers/dashboard.py and routers/devices.py. NULL-safe: `entity !=
        # X` alone silently drops every entity-less device too, since SQL
        # NULL != X evaluates to NULL, not TRUE.
        w.append(or_(Device.entity.is_(None), Device.entity != EXTERNAL_PARTNER_TEST_ENTITY))
    apply_date_range(w, Device.created_at, date_from, date_to)
    return w


@router.get("/stock/data")
async def stock_in_data(
    request: Request,
    draw: int = 1, start: int = 0, length: int = 25,
    device_type: str = "", lot_number: str = "", date_from: str = "", date_to: str = "",
    entity: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """DataTables server-side feed for Inventory Manager's Inventory Stock table.

    Only ~50 rows in production today, so this page was never close to
    freezing — converted for consistency with the other 7 pages and so it
    picks up the same Show Entries options. GRN/Invoice were hidden with a
    d-none class on both header and cell; that pairing is fragile the moment
    rows stop being rendered inline (it is exactly what broke the Sales list),
    so both columns now hide via DataTables' own columnDefs instead.
    """
    from sqlalchemy import desc as _desc, asc as _asc
    from html import escape

    page_filters = _stock_filters(device_type, lot_number, date_from, date_to, entity=entity)
    base = select(Device, Lot.lot_number).join(Lot, Device.lot_id == Lot.id).where(*page_filters)
    count_base = select(func.count()).select_from(Device).join(Lot, Device.lot_id == Lot.id).where(*page_filters)

    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = []
    if search:
        like = f"%{search}%"
        search_filters.append(or_(
            Device.barcode.ilike(like), Device.serial_no.ilike(like), Device.brand.ilike(like),
            Device.model.ilike(like), Lot.lot_number.ilike(like),
            Device.grn_number.ilike(like), Device.invoice_number.ilike(like),
        ))

    total = (await db.execute(count_base)).scalar() or 0
    # Same as the other server-side feeds: skip the identical second count when
    # there is no search term narrowing the set.
    filtered = total if not search_filters else (
        (await db.execute(count_base.where(*search_filters))).scalar() or 0)

    # Column 3 is Location ID (inserted after Lot) — every index from Brand
    # onward shifts by one to make room for it. RAM/Storage columns were
    # removed from the table entirely, so Grade moved from 9 to 7.
    # Column 9 (Assigned User) sorts by the same timestamp that badge is
    # actually derived from — the latest StockTransfer.transfer_date with a
    # department set — so "Date Assigned" is a real value, not a stand-in
    # like Device.updated_at, which also moves on edits that have nothing to
    # do with assignment.
    latest_assigned_at = (
        select(func.max(StockTransfer.transfer_date))
        .where(StockTransfer.device_id == Device.id, StockTransfer.department.isnot(None))
        .correlate(Device)
        .scalar_subquery()
    )
    # Column 9 is "Returned" (2026-10-10, replaced Assigned User): sorts by
    # whether the tag has any Return on record. With no explicit sort the
    # table keeps its old default — latest assignment first.
    has_return = (
        select(func.count(Return.id)).where(Return.device_id == Device.id)
        .correlate(Device).scalar_subquery()
    )
    col_map = {1: Device.barcode, 2: Lot.lot_number, 4: Device.brand, 5: Device.model,
               6: Device.device_type, 7: Device.grade, 9: has_return}
    order_param = request.query_params.get("order[0][column]")
    try:
        order_col = int(order_param) if order_param is not None else None
    except ValueError:
        order_col = None
    order_dir = request.query_params.get("order[0][dir]", "desc")
    if order_col is None:
        order_by = _desc(latest_assigned_at).nulls_last()
    else:
        sort_expr = col_map.get(order_col, Device.updated_at)
        order_by = _asc(sort_expr) if order_dir == "asc" else _desc(sort_expr)

    rows = (await db.execute(
        base.where(*search_filters).order_by(order_by, Device.barcode)
        .offset(max(0, start)).limit(min(max(1, length), 5000))
    )).all()

    device_ids = [d.id for d, _ in rows]

    # Location ID column — same source as Device Detail's own "Location ID"
    # row: the latest DeviceLocationLog entry (what "assign one" / the
    # pickup-placeback flow at /locations/device/{id} actually writes), with
    # device.location_id as a fallback for a device assigned only via Edit
    # Device's dropdown and never through that flow.
    from routers.devices import _build_location_map
    location_map = {}
    if device_ids:
        log_map = await _build_location_map(db, device_ids)
        location_map = {did: v["unit_id"] for did, v in log_map.items() if v.get("unit_id")}
        missing = [d for d in device_ids if str(d) not in location_map]
        if missing:
            for did, unit_id in (await db.execute(
                select(Device.id, StorageLocation.unit_id)
                .join(StorageLocation, Device.location_id == StorageLocation.id)
                .where(Device.id.in_(missing))
            )).all():
                location_map[str(did)] = unit_id

    # Returned column: latest Return per device (Reason / Condition / Complaint
    # shown in the info-icon tooltip).
    return_map = {}
    if device_ids:
        for r in (await db.execute(
            select(Return).where(Return.device_id.in_(device_ids))
            .order_by(Return.return_date.desc())
        )).scalars().all():
            return_map.setdefault(str(r.device_id), r)

    def esc(v):
        return escape(str(v)) if v is not None else ""

    def returned_cell(r):
        if not r:
            return '<span class="badge bg-light text-dark border">No</span>'
        tip = (f"Reason: {r.reason or '—'}\nCondition: {r.condition_on_return or '—'}\n"
               f"Complaint: {r.complaint_text or '—'}")
        return (f'<span class="badge bg-warning text-dark">Yes</span>'
                f'<i class="bi bi-info-circle text-primary ms-1" style="cursor:help" title="{esc(tip)}"></i>')

    parts_badges = await parts_badge_map(db, device_ids)

    # Row buttons follow the Permission Matrix's Stock Inwards > Edit tick (2026-10-25)
    from models.role_permissions import has_perm as _hp
    _role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    _can_move = _hp(_role, "stock", "edit")
    data = []
    for d, lot_number_val in rows:
        data.append([
            f'<input type="checkbox" class="form-check-input stockChk" value="{esc(d.barcode)}">',
            (f'<span class="text-nowrap"><a href="/devices/{esc(d.barcode)}" class="font-monospace text-decoration-none">'
             f'<code>{esc(d.barcode)}</code></a>{flag_badge_html(d)}{parts_badges.get(str(d.id), "")}</span>'
             f'<div style="color:#999999;font-size:12px;">{esc(d.entity) if d.entity else "—"}</div>'),
            f'<span class="badge bg-info text-dark">{esc(lot_number_val)}</span>',
            (f'<span class="font-monospace small">{esc(location_map[str(d.id)])}</span>'
             if str(d.id) in location_map else
             f'<a href="/locations/device/{d.id}" class="btn btn-xs btn-outline-primary py-0 px-2" '
             f'style="font-size:.75rem;">Assign</a>'),
            esc(d.brand or "—"), esc(d.model or "—"), esc(d.device_type or "—"),
            esc(d.grade or "—"),
            f'<span class="bkt-cell" data-barcode="{esc(d.barcode)}"><span class="text-muted">—</span></span>',
            returned_cell(return_map.get(str(d.id))),
            # Every row here is already at Stock In (see _stock_filters), so
            # both actions are always valid. Move to Production is hidden
            # (d-none, not removed — same form/endpoint still works if it's
            # ever unhidden again) and Move to L1/L2 is the primary-styled
            # action now (2026-09-18).
            ('<span class="text-muted">—</span>' if not _can_move else
             f'<form method="post" action="/stock/move-to-trc" class="d-inline d-none">'
             f'<input type="hidden" name="csrf_token" value="{esc(request.cookies.get("csrf_token", ""))}">'
             f'<input type="hidden" name="barcode" value="{esc(d.barcode)}">'
             f'<button type="submit" class="btn btn-sm btn-outline-info py-0 px-2 mb-1"><i class="bi bi-cpu"></i> Move to Production</button>'
             f'</form>'
             f'<form method="post" action="/stock/move-to-l1l2" class="d-inline">'
             f'<input type="hidden" name="csrf_token" value="{esc(request.cookies.get("csrf_token", ""))}">'
             f'<input type="hidden" name="barcode" value="{esc(d.barcode)}">'
             f'<button type="submit" class="btn btn-sm btn-primary py-0 px-2"><i class="bi bi-tools"></i> Move to L1/L2</button>'
             f'</form>'),
            esc(d.grn_number or ""), esc(d.invoice_number or ""),
        ])

    return {"draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data}


def _parse_barcodes(raw: str) -> list:
    seen, out = set(), []
    for b in (raw or "").replace("\n", ",").split(","):
        b = b.strip()
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


@router.get("/stock/flag-info")
async def stock_flag_info(
    barcodes: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Flag Items modal prefill: how many of the selected tags are flagged, and
    the flag details when every flagged one shares the same values (so Edit
    starts from them)."""
    codes = _parse_barcodes(barcodes)
    if not codes:
        return {"ok": True, "selected": 0, "flagged": 0, "common": None}
    devices = (await db.execute(select(Device).where(Device.barcode.in_(codes)))).scalars().all()
    flagged = [d for d in devices if is_flagged(d)]
    common = None
    if flagged:
        vals = {(d.flag_authorized_by or "", d.flag_request_date, d.flag_target_date, d.flag_reason or "")
                for d in flagged}
        if len(vals) == 1:
            a, rq, tg, rs = next(iter(vals))
            common = {"authorized_by": a,
                      "request_date": rq.isoformat() if rq else "",
                      "target_date": tg.isoformat() if tg else "",
                      "reason": rs}
    return {"ok": True, "selected": len(devices), "flagged": len(flagged), "common": common}


@router.post("/stock/flag")
async def stock_flag_save(
    request: Request,
    barcodes: str = Form(...),
    action: str = Form("save"),
    authorized_by: str = Form(""),
    request_date: str = Form(""),
    target_date: str = Form(""),
    reason: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Flag Items (Urgent at inventory) — add/edit (action=save) or remove
    (action=remove) the flag on every selected tag. The flag also clears
    itself when a tag moves back into Stock In (models/device.py)."""
    from datetime import date as _date
    codes = _parse_barcodes(barcodes)
    if not codes:
        return JSONResponse({"ok": False, "error": "No tags selected."}, status_code=400)
    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(codes), Device.is_trashed.isnot(True))
    )).scalars().all()
    if not devices:
        return JSONResponse({"ok": False, "error": "None of the selected tags were found."}, status_code=404)

    if action == "remove":
        for d in devices:
            d.flag_authorized_by = d.flag_request_date = d.flag_target_date = None
            d.flag_reason = d.flagged_at = d.flagged_by = None
        await audit(db, user=current_user, action="DEVICE_FLAG_REMOVED", table_name="devices",
                    record_id=f"bulk:{len(devices)}",
                    new_value={"barcodes": [d.barcode for d in devices][:200]}, request=request)
        await db.commit()
        return {"ok": True, "updated": len(devices), "removed": True}

    authorized_by = authorized_by.strip()
    reason = reason.strip()
    def _d(v):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return _date.fromisoformat(v)
        except ValueError:
            raise HTTPException(400, f"Invalid date: {v}")
    try:
        rq, tg = _d(request_date), _d(target_date)
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=400)
    missing = [n for n, v in (("Flag Authorized By", authorized_by), ("Flag Request Date", rq),
                              ("Flag Target Date", tg), ("Flag Reason", reason)) if not v]
    if missing:
        return JSONResponse({"ok": False, "error": "Required: " + ", ".join(missing)}, status_code=400)
    if tg < rq:
        return JSONResponse({"ok": False, "error": "Flag Target Date can't be before the Request Date."},
                            status_code=400)
    now = app_now()
    for d in devices:
        d.flag_authorized_by = authorized_by[:100]
        d.flag_request_date = rq
        d.flag_target_date = tg
        d.flag_reason = reason
        d.flagged_at = now
        d.flagged_by = current_user.username
    await audit(db, user=current_user, action="DEVICE_FLAG_SET", table_name="devices",
                record_id=f"bulk:{len(devices)}",
                new_value={"barcodes": [d.barcode for d in devices][:200], "authorized_by": authorized_by,
                           "request_date": rq.isoformat(), "target_date": tg.isoformat(), "reason": reason},
                request=request)
    await db.commit()
    return {"ok": True, "updated": len(devices)}


@router.get("/stock", response_class=HTMLResponse)
async def stock_in_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    device_type: str = Query(default=""),
    lot_number: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    entity: str = Query(default=""),
):
    # Rows for the Inventory Stock table come from /stock/data (DataTables
    # server-side); only the count is needed here.
    stock_filters = _stock_filters(device_type, lot_number, date_from, date_to, entity=entity)
    count_stmt = (
        select(func.count())
        .select_from(Device)
        .join(Lot, Device.lot_id == Lot.id)
        .where(*stock_filters)
    )
    total = (await db.execute(count_stmt)).scalar() or 0

    # ── Analytics count cards (#17) ──────────────────────────────────────────
    count_rows = (await db.execute(
        select(Device.current_stage, func.count(Device.id))
        .where(Device.is_active == True, Device.is_trashed == False)
        .group_by(Device.current_stage)
    )).all()
    stage_counts = {stage: cnt for stage, cnt in count_rows}

    def _c(*stages):
        return sum(stage_counts.get(s, 0) for s in stages)

    repair_stages = [DeviceStage.l1, DeviceStage.l2, DeviceStage.l3, DeviceStage.qc_check]
    cosmetic_stages = [DeviceStage.cosmetic_received, DeviceStage.cleaning, DeviceStage.dry_sanding,
                       DeviceStage.masking, DeviceStage.painting, DeviceStage.water_sanding,
                       DeviceStage.cosmetic_completed, DeviceStage.final_qc]
    analytics = {
        "iqc": _c(DeviceStage.iqc),
        "in_stock": _c(*repair_stages, *cosmetic_stages, DeviceStage.ready_to_sale),
        "trc": _c(*repair_stages, *cosmetic_stages),
        "ready": _c(DeviceStage.ready_to_sale),
    }

    # ── Entity total cards (slim row above the 4 analytics cards) ───────────
    # Same scope as the analytics cards above: every active device, regardless
    # of the page's own filters — a whole-inventory total per entity, not a
    # Stock-In-stage-scoped count.
    entity_option_list = await entity_values(db)
    entity_total_rows = (await db.execute(
        select(Device.entity, func.count(Device.id))
        .where(Device.is_active == True)  # noqa: E712
        .group_by(Device.entity)
    )).all()
    entity_total_map = {e: c for e, c in entity_total_rows}
    entity_totals = [(e, entity_total_map.get(e, 0)) for e in entity_option_list]

    # Assigned department, Cost & Parts and Location maps used to be built here
    # from a full device fetch, purely to feed the old inline table's cells.
    # /stock/data now builds the department map itself, per page; Cost & Parts
    # and its Movement table were removed from this page previously and
    # nothing in the template reads cost_parts_map or location_map any more.

    # Lot Number filter options — every lot that still has stock-in devices,
    # independent of the currently applied filters so the dropdown never
    # narrows itself down to the single selected value.
    lot_number_options = [
        ln for (ln,) in (await db.execute(
            select(Lot.lot_number)
            .join(Device, Device.lot_id == Lot.id)
            .where(Device.current_stage == DeviceStage.stock_in, Device.is_active == True)
            .distinct()
            .order_by(Lot.lot_number)
        )).all()
    ]

    storage_locations = (await db.execute(
        select(StorageLocation).where(StorageLocation.is_active == True)  # noqa: E712
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )).scalars().all()

    # Per-entity counts over the currently filtered set (not a separate
    # unfiltered query) — the summary strip above the Inventory Stock table,
    # same pattern as All Inventory's entity badges.
    entity_counts_rows = (await db.execute(
        select(Device.entity, func.count())
        .select_from(Device).join(Lot, Device.lot_id == Lot.id)
        .where(*stock_filters)
        .group_by(Device.entity)
    )).all()
    entity_counts = {(e or "Unassigned"): c for e, c in entity_counts_rows}

    # ── Final QC Pass (Buckets): devices moved here via Final QC's
    #    "Move to Inventory" action — still bucket-linked at ready_to_sale,
    #    awaiting Release Bucket. ────────────────────────────────────────────
    fqc_pass_rows = (await db.execute(
        select(Device).where(
            Device.current_stage == DeviceStage.ready_to_sale,
            Device.bucket_id.isnot(None),
            Device.is_active == True,
        )
    )).scalars().all()
    fqc_pass_bucket_ids = {d.bucket_id for d in fqc_pass_rows}
    fqc_pass_buckets_by_id = {}
    if fqc_pass_bucket_ids:
        b_rows = (await db.execute(select(Bucket).where(Bucket.id.in_(fqc_pass_bucket_ids)))).scalars().all()
        fqc_pass_buckets_by_id = {b.id: b for b in b_rows}
    fqc_pass_grouped = {}
    for d in fqc_pass_rows:
        b = fqc_pass_buckets_by_id.get(d.bucket_id)
        if not b:
            continue
        fqc_pass_grouped.setdefault(b.id, {"bucket_id": str(b.id), "bucket_name": b.name or b.bucket_number, "bucket_number": b.bucket_number})
    fqc_pass_buckets = list(fqc_pass_grouped.values())

    # ── Return Stock: every tag with Device.return_status=True (set at
    #    /returns/new submission time, regardless of manager approval — see
    #    routers/sales.py process_return), joined to its Reason/Condition/
    #    Complaint straight from that same form's Return row. A device can
    #    carry more than one Return over its life (rare); keep only the
    #    newest by return_date — approved_at is null for pending returns, so
    #    that field can't be used to order here. Excludes tags where a
    #    replacement was already issued (Device.tag_return_status starts
    #    with "Replaced by " — set by process_return's Replace Now) — that
    #    tag's own repair is no longer pending, the replacement tag is what
    #    the customer actually has, so it has no business sitting in Return
    #    Stock (2026-09-24 fix; the `or_` keeps the common NULL/"Return for
    #    Repair"/"Return for Credit Note" cases in, since `NOT (NULL LIKE
    #    ...)` is NULL, not true, and would otherwise silently drop every
    #    return whose tag_return_status was never set to "Replaced by "). ──
    return_join_rows = (await db.execute(
        select(Device, Lot.lot_number, Return)
        .join(Lot, Device.lot_id == Lot.id)
        .join(Return, Return.device_id == Device.id)
        .where(Device.return_status == True, Device.is_active == True,  # noqa: E712
               or_(Device.tag_return_status.is_(None),
                   Device.tag_return_status.notlike('Replaced by %')))
        .order_by(Return.return_date.desc())
    )).all()
    return_stock_by_device = {}
    for dev, lot_num, ret in return_join_rows:
        return_stock_by_device.setdefault(dev.id, (dev, lot_num, ret))

    return_device_ids = list(return_stock_by_device.keys())
    part_cost_map = {}
    if return_device_ids:
        part_cost_rows = (await db.execute(
            select(SparePartConsumption.device_id,
                   func.coalesce(func.sum(SparePartConsumption.total_cost), 0))
            .where(SparePartConsumption.device_id.in_(return_device_ids))
            .group_by(SparePartConsumption.device_id)
        )).all()
        part_cost_map = {did: cost for did, cost in part_cost_rows}

    return_stock_rows = []
    for dev, lot_num, ret in return_stock_by_device.values():
        # "Part Cost (if out of warranty then add value of Spare Part consumed
        # in this tag)" — under-warranty returns don't bill the part back onto
        # this cost line.
        part_cost = part_cost_map.get(dev.id, 0) if ret.warranty_status == "out_of_warranty" else 0
        # Labour Cost defaults to the Paid Repair amount captured on the
        # Internal Tag tab (Return.refund_amount, only ever set when that
        # form's "Paid Repair" row is shown — i.e. out-of-warranty) until
        # someone Verifies it with an actual figure.
        labour_cost = ret.labour_cost if ret.labour_cost is not None else (
            ret.refund_amount if ret.warranty_status == "out_of_warranty" else None
        )
        return_stock_rows.append({
            "device": dev, "lot_number": lot_num, "ret": ret, "part_cost": part_cost,
            "labour_cost": labour_cost,
            "stage_label": STAGE_LABELS.get(dev.current_stage, dev.current_stage.value if dev.current_stage else "—"),
        })
    return_stock_rows.sort(key=lambda r: r["ret"].return_date or app_now(), reverse=True)

    # Flag Items modal: "Flag Authorized By" = active users' display names.
    flag_users = sorted({(u.full_name or u.username) for u in (await db.execute(
        select(User).where(User.status == True)
    )).scalars().all() if (u.full_name or u.username)})
    return templates.TemplateResponse("lots/stock_in.html", {
        "request": request, "current_user": current_user,
        "flag_users": flag_users,
        "analytics": analytics,
        "departments": STOCK_DEPARTMENTS,
        "total": total,
        "device_type": device_type,
        "device_type_options": await master_values(db, "device_type"),
        # Consumed by the shared _customise_modal.html "Move to Stage" dropdown.
        "stage_options": [(s.value, STAGE_LABELS.get(s, s.value)) for s in DROPDOWN_STAGES],
        # Consumed by the shared _customise_modal.html "Location ID" dropdown.
        "storage_locations": storage_locations,
        "lot_number": lot_number,
        "lot_number_options": lot_number_options,
        "entity": entity,
        "entity_options": entity_option_list,
        "entity_counts": entity_counts,
        "entity_totals": entity_totals,
        "zone_options": [
            (z.value, ZONE_LABELS.get(z, z.value))
            for z in [ZoneType.workshop, ZoneType.holding, ZoneType.dispatch, ZoneType.showroom, ZoneType.warehouse]
        ],
        "unit_type_options": [(u.value, UNIT_TYPE_LABELS.get(u, u.value)) for u in UnitType],
        "fqc_pass_buckets": fqc_pass_buckets,
        "return_stock_rows": return_stock_rows,
    })


async def _latest_return_for_device(db: AsyncSession, device_id) -> "Return | None":
    """Most recent Return row for this device regardless of approval_status —
    Verify/Complete act on whatever /returns/new captured, not gated on the
    separate manager-approval workflow (see Return Stock query above)."""
    return (await db.execute(
        select(Return).where(Return.device_id == device_id)
        .order_by(Return.return_date.desc()).limit(1)
    )).scalars().first()


@router.post("/stock/return-stock/assign-bucket")
async def return_stock_assign_bucket(
    request: Request,
    barcodes: str = Form(...),
    bucket_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Assign selected Return Stock tags to a Bucket/Carton — reuses an
    existing bucket by name (case-insensitive), same lookup-or-create pattern
    as routers/cosmetic.py's Final QC Fail bucketing, so repeatedly assigning
    to "Carton A1" keeps accumulating into one bucket rather than making a
    new one every time. Marked is_customer_return so it carries the "Customer
    Return" label in the Buckets/Cartons table below.
    """
    barcode_list = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not barcode_list:
        raise HTTPException(400, "No tags selected")
    name = bucket_name.strip()
    if not name:
        raise HTTPException(400, "Bucket Name is required")

    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(barcode_list), Device.is_active == True)  # noqa: E712
    )).scalars().all()
    if not devices:
        raise HTTPException(404, "No matching devices found")

    bucket = (await db.execute(
        select(Bucket).where(func.lower(Bucket.name) == name.lower())
    )).scalars().first()
    if not bucket:
        bucket = Bucket(name=name, bucket_number=_new_bucket_number(),
                         created_by=current_user.username, is_customer_return=True)
        db.add(bucket)
        await db.flush()
    elif not bucket.is_customer_return:
        bucket.is_customer_return = True

    for dev in devices:
        dev.bucket_id = bucket.id
        dev.updated_at = app_now()

    await audit(db, user=current_user, action="RETURN_STOCK_BUCKET_ASSIGNED",
                table_name="buckets", record_id=str(bucket.id),
                new_value={"barcodes": barcode_list, "bucket_number": bucket.bucket_number},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "bucket_number": bucket.bucket_number, "bucket_id": str(bucket.id)})


@router.post("/stock/return-stock/{barcode}/verify")
async def return_stock_verify(
    request: Request,
    barcode: str,
    repair_cost: str = Form("0"),
    labour_cost: str = Form("0"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")
    ret = await _latest_return_for_device(db, device.id)
    if not ret:
        raise HTTPException(404, "No return found for this tag")

    try:
        ret.repair_cost = Decimal(repair_cost) if repair_cost.strip() else Decimal("0")
        ret.labour_cost = Decimal(labour_cost) if labour_cost.strip() else Decimal("0")
    except InvalidOperation:
        raise HTTPException(400, "Repair Cost and Labour Cost must be numbers")

    await audit(db, user=current_user, action="RETURN_STOCK_VERIFIED",
                table_name="returns", record_id=str(ret.id),
                new_value={"repair_cost": str(ret.repair_cost), "labour_cost": str(ret.labour_cost)},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "repair_cost": str(ret.repair_cost), "labour_cost": str(ret.labour_cost)})


@router.post("/stock/return-stock/{barcode}/complete")
async def return_stock_complete(
    request: Request,
    barcode: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Mark a Return Stock tag sold again, reusing the customer details from
    the sale this same tag was returned from — mirrors create_sale's core
    fields (routers/sales.py) rather than sending the operator through the
    New Tag Sale form a second time for a customer/tag combination already on
    file.

    The new Sale row created below is deliberately the ONLY thing this does
    to reach /gate-pass — that page (routers/gate_pass.py) already lists
    every Sale unconditionally, so this tag appears there with no extra
    wiring, carrying a "Customer Return" label under its Tag Number for as
    long as ANY Return row exists for the device (independent of
    return_status, which we reset to False below).
    """
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")
    ret = await _latest_return_for_device(db, device.id)
    if not ret:
        raise HTTPException(404, "No return found for this tag")

    orig_sale = (await db.execute(select(Sale).where(Sale.id == ret.sale_id))).scalar_one_or_none()
    if not orig_sale:
        raise HTTPException(404, "Original sale record not found for this return")

    sold_at = app_now()
    # orig_sale.warranty_days already holds the exact day count for whatever
    # Master Data label was picked at the original sale — compute_warranty_expiry
    # only understands the 3 legacy slugs (30_days/6_months/1_year), so any
    # other admin-added duration (e.g. "60 Days") would silently compute no
    # expiry at all if re-derived from warranty_type here instead.
    warranty_expires_at = (sold_at + timedelta(days=orig_sale.warranty_days)
                           if orig_sale.warranty_days else None)
    sale_num = await _gen_sale_number(db)
    new_sale = Sale(
        sale_number=sale_num, device_id=device.id, sale_price=orig_sale.sale_price,
        customer_name=orig_sale.customer_name, customer_phone=orig_sale.customer_phone,
        customer_state=orig_sale.customer_state, customer_address=orig_sale.customer_address,
        payment_mode=orig_sale.payment_mode, sold_by=current_user.username,
        sales_person=orig_sale.sales_person, sold_at=sold_at,
        notes=f"Re-sale after customer return (Return {ret.id})",
        warranty_type=orig_sale.warranty_type, warranty_days=orig_sale.warranty_days,
        warranty_expires_at=warranty_expires_at,
        company_id=orig_sale.company_id, company_name=orig_sale.company_name,
        company_address=orig_sale.company_address, company_gstin=orig_sale.company_gstin,
        company_state=orig_sale.company_state, company_state_code=orig_sale.company_state_code,
        company_phone=orig_sale.company_phone, company_email=orig_sale.company_email,
    )
    db.add(new_sale)

    prev = device.current_stage
    device.current_stage = DeviceStage.sold
    device.return_status = False  # drops it out of the Return Stock table
    device.updated_at = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=DeviceStage.sold,
                         moved_by=current_user.username, notes=f"Re-sold after return — {sale_num}"))

    await audit(db, user=current_user, action="RETURN_STOCK_COMPLETED",
                table_name="sales", record_id=str(device.id),
                new_value={"sale_number": sale_num, "return_id": str(ret.id)}, request=request)
    await db.commit()
    publish(EventType.SALE_COMPLETED, {"barcode": device.barcode, "sale_number": sale_num})
    return JSONResponse({"ok": True, "sale_number": sale_num})


async def _gen_sale_number(db: AsyncSession) -> str:
    from routers.sales import ensure_sale_number_seq
    await ensure_sale_number_seq(db)
    seq = (await db.execute(text("SELECT nextval('sale_number_seq')"))).scalar()
    return f"SALE-{seq:04d}"


@router.get("/stock/move-to-stock")
async def move_to_stock_get():
    return RedirectResponse(url="/stock", status_code=302)


def _transfer_snapshot(device, **over):
    """Build common StockTransfer snapshot kwargs from a device."""
    base = dict(
        device_id=device.id, barcode=device.barcode, serial_no=device.serial_no,
        make=device.brand, model=device.model, category=device.sub_category,
        product_stage=device.current_stage.value if device.current_stage else None,
        transfer_date=app_now(),
    )
    base.update(over)
    return base


@router.post("/stock/validate")
async def stock_validate(
    request: Request,
    barcode: str = Form(...),
    qty_received: str = Form(""),
    condition_received: str = Form(""),
    reassign_department: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    try:
        qty = int(qty_received) if qty_received else None
    except ValueError:
        qty = None
    db.add(StockValidation(
        device_id=device.id, barcode=device.barcode, qty_received=qty,
        condition_received=condition_received or None,
        reassign_department=reassign_department or None, notes=notes or None,
        validated_by=current_user.username,
    ))
    # Reassignment records a stock transfer to that department (drives 'Assigned User')
    if reassign_department:
        wh = device.warehouse or "Stock In"
        db.add(StockTransfer(**_transfer_snapshot(
            device, transfer_type="reassign", source="stock_reassign",
            from_warehouse=wh, to_warehouse=wh,
            transferred_by=current_user.username, department=reassign_department,
            notes="Reassigned via Stock Validate", created_by=current_user.username,
        )))
    await audit(db, user=current_user, action="STOCK_VALIDATE", table_name="stock_validations",
                record_id=str(device.id),
                new_value={"barcode": barcode, "qty_received": qty,
                           "condition": condition_received, "reassign": reassign_department},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/stock?success=Stock+validated+for+{barcode}", status_code=302)


@router.post("/stock/bulk-transfer")
async def stock_bulk_transfer(
    request: Request,
    barcodes: list[str] = Form(default=[]),
    transfer_type: str = Form("transfer_to_trc"),
    department: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    barcodes = [b for b in (barcodes or []) if b]
    if not barcodes:
        return RedirectResponse(url="/stock?error=No+items+selected", status_code=302)
    devices = (await db.execute(select(Device).where(Device.barcode.in_(barcodes)))).scalars().all()
    cnt = 0
    for device in devices:
        db.add(StockTransfer(**_transfer_snapshot(
            device, transfer_type=transfer_type or "transfer_to_trc",
            source="stock_bulk_transfer",
            from_warehouse=device.warehouse or "Stock In", to_warehouse=device.warehouse or "TRC",
            transferred_by=current_user.username, department=department or None,
            notes="Bulk stock transfer", created_by=current_user.username,
        )))
        cnt += 1
    await audit(db, user=current_user, action="STOCK_BULK_TRANSFER", table_name="stock_transfers",
                record_id=None, new_value={"count": cnt, "department": department}, request=request)
    await db.commit()
    return RedirectResponse(url=f"/stock?success={cnt}+item(s)+transferred", status_code=302)


@router.post("/stock/move-to-stock")
async def move_to_stock(
    barcode: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    prev_stage = device.current_stage
    device.current_stage = DeviceStage.stock_in
    device.updated_at = app_now()
    movement = StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.stock_in,
        moved_by=current_user.username, notes=notes or "Moved to Stock"
    )
    db.add(movement)
    await ensure_stage_location(db, device, DeviceStage.stock_in, current_user)
    await db.commit()
    return RedirectResponse(url="/stock?success=Moved+to+Stock", status_code=302)


@router.post("/stock/move-to-trc")
async def move_to_trc(
    barcode: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Move a Stock Inward device into the TRC Production stage."""
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    if device.current_stage != DeviceStage.stock_in:
        # Defense-in-depth: the row shouldn't even render this button unless
        # the device is at Stock In (see _stock_filters), but a stale page
        # or a race with another stage move could still hit this endpoint.
        return RedirectResponse(
            url="/stock?error=Device+is+not+at+Stock+In+stage", status_code=302)
    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == prev_stage,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()
    device.current_stage = DeviceStage.trc_production
    device.updated_at = app_now()
    db.add(StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.trc_production,
        moved_by=current_user.username, notes="Moved to TRC Production",
    ))
    await db.commit()
    return RedirectResponse(url="/stock?success=Moved+to+TRC+Production", status_code=302)


async def _move_device_to_l1l2(db: AsyncSession, device: Device, current_user: User) -> bool:
    """Shared by the single-row and bulk Move to L1/L2 actions. Moves a Stock
    In device into the merged L1/L2 queue (unassigned — no WorkOrder, so it
    lands in the unclaimed pool and shows the "Pick This" button on
    /repair/l1, same as any other tag arriving there without an engineer
    already picked). Returns False (no-op) for a device that isn't at Stock
    In, same defense-in-depth as move_to_trc.

    A device that cycled through L1/L2 before and was later sent back to
    Stock In without its WorkOrder ever completing (a known pattern in real
    data — e.g. Return Stock / Change Entity moving it out mid-repair) still
    carries that old pending/in_progress "l1" WorkOrder. /repair/l1's queue
    keys off "does this device have a non-completed l1 WorkOrder", so
    without closing that stale one out first, the tag would land back in
    the queue still showing the OLD WorkID + engineer instead of Pick This
    (2026-09-18 — reported as "Pick This not showing"). Same close-out
    pattern as routers/cosmetic.py's own stale-pending cleanup."""
    if device.current_stage != DeviceStage.stock_in:
        return False
    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == prev_stage,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()
    await db.execute(
        update(WorkOrder)
        .where(WorkOrder.device_id == device.id, WorkOrder.stage == "l1",
               wo_open())
        .values(status="completed", completed_at=app_now())
    )
    device.current_stage = DeviceStage.l1
    reset_repair_cycle_on_entry(device, prev_stage, DeviceStage.l1)
    device.updated_at = app_now()
    db.add(StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.l1,
        moved_by=current_user.username, notes="Moved to L1/L2 Repair",
    ))
    # Arrived at L1/L2 unassigned: one pending WorkID, to be picked (2026-10-27).
    from services.stage_work_orders import open_for_stage as _open_for_stage
    await _open_for_stage(db, device, DeviceStage.l1, created_by=current_user.username)
    return True


@router.post("/stock/move-to-l1l2")
async def move_to_l1l2(
    barcode: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Move a single Stock Inward device into the L1/L2 queue."""
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    if not await _move_device_to_l1l2(db, device, current_user):
        return RedirectResponse(
            url="/stock?error=Device+is+not+at+Stock+In+stage", status_code=302)
    await db.commit()
    return RedirectResponse(url="/stock?success=Moved+to+L1%2FL2", status_code=302)


@router.post("/stock/bulk-move-to-l1l2")
async def bulk_move_to_l1l2(
    barcodes: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Move several Stock Inward devices into the L1/L2 queue at once —
    the "Bulk Move to L1/L2" button in Inventory Stock's card header."""
    bc_list = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not bc_list:
        raise HTTPException(400, "No tags selected")
    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(bc_list))
    )).scalars().all()
    moved = 0
    for device in devices:
        if await _move_device_to_l1l2(db, device, current_user):
            moved += 1
    await db.commit()
    return JSONResponse({"ok": True, "moved": moved, "requested": len(bc_list)})


def _prod_tile_criteria():
    """Single source of truth for the Production Manager summary tiles:
    tile key -> (label, where-clause). Used by BOTH the tile counts in
    trc_production_list and the per-tile CSV (trc_production_tile_summary),
    so a tile's number and its export can never disagree. Ignores the
    page's date filter, like the tile counts."""
    active = and_(Device.is_active == True, Device.is_trashed == False)
    final_qc_stages = [DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold]
    returned_pipeline_stages = (
        [DeviceStage.trc_production, DeviceStage.l1, DeviceStage.l2, DeviceStage.l3, DeviceStage.qc_check]
        + list(COSMETIC_STAGES) + final_qc_stages
    )
    return {
        "at-you": ("Total Tags at You", and_(Device.current_stage == DeviceStage.trc_production, active)),
        "l1-l2": ("Total Tags in L1/L2", and_(Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2]), active)),
        "l3-l4": ("Total Tags in L3/L4", and_(Device.current_stage == DeviceStage.l3, active)),
        "stress": ("Total Tags in Stress", and_(Device.current_stage == DeviceStage.qc_check, active)),
        "cosmetic": ("Total Tags in Cosmetic", and_(Device.current_stage.in_(COSMETIC_STAGES), active)),
        "final-qc": ("Total Tags in Final QC", and_(Device.current_stage.in_(final_qc_stages), active)),
        # Internal Tag tab devices sent for repair, anywhere across the
        # pipeline the other tiles cover (At You through Final QC).
        "returned": ("Tags Returned", and_(
            Device.tag_return_status == "Return for Repair",
            Device.current_stage.in_(returned_pipeline_stages), active)),
    }


@router.get("/trc-production", response_class=HTMLResponse)
async def trc_production_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
):
    from utils.date_filter import apply_date_range
    _trc_filters = [Device.current_stage == DeviceStage.trc_production,
                    Device.is_active == True, Device.is_trashed == False]
    apply_date_range(_trc_filters, Device.created_at, date_from, date_to)
    base_stmt = (
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(*_trc_filters)
    )
    devices = (await db.execute(
        base_stmt.order_by(Device.updated_at.desc())
    )).all()
    total = len(devices)

    device_ids = [d.id for d, _ in devices]
    assigned_dept_map = {}
    if device_ids:
        st_rows = (await db.execute(
            select(StockTransfer.device_id, StockTransfer.department)
            .where(StockTransfer.device_id.in_(device_ids), StockTransfer.department != None)
            .order_by(StockTransfer.transfer_date.desc())
        )).all()
        for did, dept in st_rows:
            key = str(did)
            if key not in assigned_dept_map and dept:
                assigned_dept_map[key] = dept

    # Tag Number Allocation's "Bucket Name" column — the bucket (carton) a
    # device was originally grouped into at Stock Inward, not every device
    # here has one (loose tags / older intake paths can have bucket_id=None).
    # Keyed by DEVICE id (what the template looks up per row), not bucket id.
    bucket_map = {}
    if device_ids:
        bucket_ids = {d.bucket_id for d, _ in devices if d.bucket_id}
        if bucket_ids:
            b_rows = (await db.execute(
                select(Bucket.id, Bucket.name, Bucket.bucket_number)
                .where(Bucket.id.in_(bucket_ids))
            )).all()
            bucket_name_by_id = {bid: (name or bnum) for bid, name, bnum in b_rows}
            bucket_map = {
                str(d.id): bucket_name_by_id[d.bucket_id]
                for d, _ in devices if d.bucket_id in bucket_name_by_id
            }

    # ── Cost & Parts table data (Batch D Task 1) ─────────────────────────────
    cost_parts_map = await build_cost_parts_map(db, device_ids)
    location_map = {}
    if device_ids:
        loc_ids = {d.location_id for d, _ in devices if d.location_id}
        if loc_ids:
            loc_rows = (await db.execute(
                select(StorageLocation).where(StorageLocation.id.in_(loc_ids))
            )).scalars().all()
            location_map = {l.id: l for l in loc_rows}

    # ── "Tag Numbers in Repair Line" (Batch 8 item 25) ───────────────────────
    repair_line_devices = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2, DeviceStage.l3]),
               Device.is_active == True)
        .order_by(Device.updated_at.desc())
    )).all()

    # Assigned engineer's actual name (not just their department/role badge,
    # which the "Assigned User" column already showed) for the Repair Line
    # table's "Assigned User Name" column.
    repair_line_ids = [d.id for d, _ in repair_line_devices]
    assigned_name_map = {}
    if repair_line_ids:
        wo_rows = (await db.execute(
            select(WorkOrder.device_id, WorkOrder.assigned_name)
            .where(WorkOrder.device_id.in_(repair_line_ids), wo_open())
            .order_by(WorkOrder.device_id, WorkOrder.assigned_at.desc())
        )).all()
        for did, name in wo_rows:
            key = str(did)
            if key not in assigned_name_map and name:
                assigned_name_map[key] = name

    # Scrap Products from Repair Line — moved here from All Inventory (that
    # page now only handles active/searchable inventory, not scrap triage).
    scrap_devices = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.grade == DeviceGrade.scrap, Device.current_stage == DeviceStage.scrapped,
               Device.is_active == True)
        .order_by(Device.updated_at.desc())
    )).all()

    # ── Final QC Fail (Bucket): buckets whose devices arrived here via Final
    #    QC's "Move to Production" action — l1 stage, not yet assigned
    #    (assigned_to_production still False). Once "Assign" is used they
    #    drop off this list and appear in the ordinary "Buckets in Repair
    #    Line" allocation table instead. ───────────────────────────────────
    fqc_fail_rows = (await db.execute(
        select(Device).where(
            Device.current_stage == DeviceStage.l1,
            Device.final_qc_status == "fail",
            Device.is_active == True,
            Device.bucket_id.isnot(None),
        )
    )).scalars().all()
    fqc_fail_bucket_ids = {d.bucket_id for d in fqc_fail_rows}
    fqc_fail_buckets_by_id = {}
    if fqc_fail_bucket_ids:
        b_rows = (await db.execute(
            select(Bucket).where(Bucket.id.in_(fqc_fail_bucket_ids), Bucket.assigned_to_production == False)
        )).scalars().all()
        fqc_fail_buckets_by_id = {b.id: b for b in b_rows}
    fqc_fail_grouped = {}
    for d in fqc_fail_rows:
        b = fqc_fail_buckets_by_id.get(d.bucket_id)
        if not b:
            continue
        g = fqc_fail_grouped.setdefault(b.id, {
            "bucket_id": str(b.id), "bucket_name": b.name or b.bucket_number, "bucket_number": b.bucket_number,
            "count": 0, "failure_reason": None,
        })
        g["count"] += 1
        g["failure_reason"] = g["failure_reason"] or d.fqc_failure_reason
    fqc_fail_buckets = list(fqc_fail_grouped.values())

    # ── As-Is Lots in TRC (2026-10-15): tags carrying a Sub-Lot Number (As-Is
    #    Lot) anywhere from TRC Production through Final QC Pass/Fail Hold in
    #    the REAL flow — TRC Production hands tags to L1/L2 then L3 repair, so
    #    those are included (DeviceStage declares l1-l3 before trc_production,
    #    so a slice of the enum wrongly dropped every tag in repair). ─────────
    _asis_stages = [DeviceStage.trc_production, DeviceStage.l1, DeviceStage.l2, DeviceStage.l3,
                    DeviceStage.qc_check, *COSMETIC_STAGES, DeviceStage.final_qc,
                    DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold]
    as_is_trc = (await db.execute(
        select(Device, Lot.lot_number)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage.in_(_asis_stages),
               func.coalesce(func.trim(Device.sub_lot_number), "") != "",
               Device.is_active == True, Device.is_trashed == False)
        .order_by(Device.sub_lot_number, Device.barcode)
    )).all()
    as_is_ids = [d.id for d, _ in as_is_trc]
    as_is_bucket_map, as_is_assigned_map = {}, {}
    if as_is_ids:
        _bids = {d.bucket_id for d, _ in as_is_trc if d.bucket_id}
        if _bids:
            _bn = {bid: (name or bnum) for bid, name, bnum in (await db.execute(
                select(Bucket.id, Bucket.name, Bucket.bucket_number).where(Bucket.id.in_(_bids))
            )).all()}
            as_is_bucket_map = {str(d.id): _bn[d.bucket_id] for d, _ in as_is_trc if d.bucket_id in _bn}
        # Assigned User: the engineer on the open WorkOrder, else the latest
        # Stock Transfer department (what Tag Number Allocation shows).
        for did, name in (await db.execute(
            select(WorkOrder.device_id, WorkOrder.assigned_name)
            .where(WorkOrder.device_id.in_(as_is_ids), wo_open())
            .order_by(WorkOrder.device_id, WorkOrder.assigned_at.desc())
        )).all():
            if name and str(did) not in as_is_assigned_map:
                as_is_assigned_map[str(did)] = name
        for did, dept in (await db.execute(
            select(StockTransfer.device_id, StockTransfer.department)
            .where(StockTransfer.device_id.in_(as_is_ids), StockTransfer.department != None)
            .order_by(StockTransfer.transfer_date.desc())
        )).all():
            if dept and str(did) not in as_is_assigned_map:
                as_is_assigned_map[str(did)] = dept

    # Brand column: a tag saved with no Make shows the Make that Product Master
    # pairs with its Model (dropdown model -> parent make, else a Product row),
    # only when that model maps to exactly one make. Display only — nothing saved.
    as_is_make_map = {}
    _blank_models = {(d.model or "").strip().lower() for d, _ in as_is_trc
                     if not (d.brand or "").strip() and (d.model or "").strip()}
    if _blank_models:
        from sqlalchemy.orm import aliased
        from models.product_master import PMDropdownValue, PMProduct
        _parent = aliased(PMDropdownValue)
        _makes = {}
        for mdl, mk in (await db.execute(
            select(PMDropdownValue.value, _parent.value)
            .join(_parent, PMDropdownValue.parent_id == _parent.id)
            .where(PMDropdownValue.category == "model",
                   func.lower(PMDropdownValue.value).in_(_blank_models))
        )).all():
            _makes.setdefault(mdl.strip().lower(), set()).add(mk)
        for mdl, mk in (await db.execute(
            select(PMProduct.model, PMProduct.make)
            .where(func.lower(PMProduct.model).in_(_blank_models))
        )).all():
            _makes.setdefault(mdl.strip().lower(), set()).add(mk)
        as_is_make_map = {m: next(iter(v)) for m, v in _makes.items() if len(v) == 1}

    # ── Summary card tiles ("Total Tags at You / L1/L2 / L3/L4 / PNA / Stress
    #    / Final QC") — simple stage-scoped counts, each is_active-filtered
    #    to match every other count on this page. ─────────────────────────
    tiles_active = and_(Device.is_active == True, Device.is_trashed == False)
    tile_criteria = _prod_tile_criteria()

    async def _tile_count(key):
        return (await db.execute(
            select(func.count(Device.id)).where(tile_criteria[key][1])
        )).scalar() or 0

    tags_at_you = await _tile_count("at-you")
    tags_l1l2 = await _tile_count("l1-l2")
    tags_l3l4 = await _tile_count("l3-l4")
    tags_pna = (await db.execute(
        select(func.count(func.distinct(Device.id)))
        .join(DevicePNAPart, DevicePNAPart.device_id == Device.id)
        .where(Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2]),
               tiles_active, DevicePNAPart.is_active == True)
    )).scalar() or 0
    tags_stress = await _tile_count("stress")
    tags_final_qc = await _tile_count("final-qc")
    tags_cosmetic = await _tile_count("cosmetic")
    tags_returned = await _tile_count("returned")

    return templates.TemplateResponse("lots/trc_production.html", {
        "request": request, "devices": devices, "current_user": current_user,
        "assigned_dept_map": assigned_dept_map, "bucket_map": bucket_map, "departments": STOCK_DEPARTMENTS,
        "cost_parts_map": cost_parts_map, "location_map": location_map,
        "repair_line_devices": repair_line_devices, "assigned_name_map": assigned_name_map,
        "total": total, "scrap_devices": scrap_devices,
        "fqc_fail_buckets": fqc_fail_buckets,
        "as_is_trc": as_is_trc, "as_is_bucket_map": as_is_bucket_map,
        "as_is_assigned_map": as_is_assigned_map, "as_is_make_map": as_is_make_map,
        "tags_at_you": tags_at_you, "tags_l1l2": tags_l1l2, "tags_l3l4": tags_l3l4,
        "tags_pna": tags_pna, "tags_stress": tags_stress, "tags_final_qc": tags_final_qc,
        "tags_cosmetic": tags_cosmetic, "tags_returned": tags_returned,
        "change_engineer_stages": [(s.value, label) for s, label in CHANGE_ENGINEER_STAGES],
    })


@router.get("/trc-production/summary/{tile}")
async def trc_production_tile_summary(
    tile: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """CSV for a Production Manager summary tile's "Summary" button — exactly
    the tags that tile counts (shared criterion via _prod_tile_criteria)."""
    criteria = _prod_tile_criteria().get(tile)
    if criteria is None:
        raise HTTPException(status_code=404, detail="Unknown tile")
    is_returned = tile == "returned"
    result = await db.execute(
        select(Device.barcode, Device.brand, Device.model, Device.current_stage,
               Device.updated_at, Device.tag_return_status, Lot.lot_number)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(criteria[1])
        .order_by(Device.barcode)
    )

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Tag Number", "Brand", "Model", "Lot Number", "Stage", "Updated At"]
    if is_returned:
        header.append("Return Status")
    writer.writerow(header)
    for barcode, brand, model, stage, updated_at, return_status, lot_number in result.all():
        row = [
            barcode or "", brand or "", model or "", lot_number or "",
            STAGE_LABELS.get(stage, stage.value if stage else ""),
            updated_at.strftime("%d-%m-%Y %H:%M") if updated_at else "",
        ]
        if is_returned:
            row.append(return_status or "")
        writer.writerow(row)
    output.seek(0)
    return Response(
        content=output.getvalue().encode(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trc_{tile.replace('-', '_')}_summary.csv"},
    )


@router.get("/trc-production/pna-summary")
async def trc_production_pna_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """CSV for the Production Manager PNA tile's "Summary" button — every
    active PNA-marked part for devices currently in L1/L2 or L3/L4 Repair,
    one row per (tag, part). Deliberately broader than the tile's own count
    above (tags_pna is L1/L2 only) since L3/L4 was explicitly asked for
    here — the tile's number and this export's tag count won't match 1:1,
    that's intended scope, not a bug."""
    result = await db.execute(
        select(DevicePNAPart, Device.barcode, Device.brand, Device.model,
               Device.current_stage, Lot.lot_number)
        .join(Device, DevicePNAPart.device_id == Device.id)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(
            Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2, DeviceStage.l3]),
            Device.is_active == True, Device.is_trashed == False,
            DevicePNAPart.is_active == True,
        )
        .order_by(Device.barcode, DevicePNAPart.part_name)
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Tag Number", "Brand", "Model", "Lot Number", "Stage",
                      "Part Name", "Part Category", "Marked By", "Marked At"])
    for pna, barcode, brand, model, stage, lot_number in result.all():
        writer.writerow([
            barcode or pna.barcode or "", brand or "", model or "", lot_number or "",
            STAGE_LABELS.get(stage, stage.value if stage else ""),
            pna.part_name, pna.part_category or "",
            pna.marked_by or "", pna.marked_at.strftime("%d-%m-%Y %H:%M") if pna.marked_at else "",
        ])
    output.seek(0)
    return Response(
        content=output.getvalue().encode(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pna_tag_summary.csv"},
    )


# Production Manager's "Change Engineer" modal — target stages it can move a
# tag into. Scoped to the repair pipeline this page is about (not every
# DeviceStage — moving a tag straight to e.g. "sold" via a reassignment tool
# would be nonsensical), default L1 per spec. WorkOrder.stage is VARCHAR(5)
# (see models/work_order.py), so every code here is pre-truncated to fit —
# same convention as routers/cosmetic.py's MOVE_STAGE_CODE.
CHANGE_ENGINEER_STAGES = [
    (DeviceStage.l1, STAGE_LABELS[DeviceStage.l1]),
    (DeviceStage.l3, STAGE_LABELS[DeviceStage.l3]),
    (DeviceStage.qc_check, "Stress Test"),
    # "Cosmetic" targets the first of the 8 combined cosmetic stages
    # (cosmetic_received) — same simplification already used for L1/L2 (->
    # DeviceStage.l1) and L3/L4 (-> DeviceStage.l3): one concrete DeviceStage
    # stands in for the whole grouped phase, since a device's current_stage
    # is always exactly one enum value, never a bucket.
    (DeviceStage.cosmetic_received, "Cosmetic"),
    (DeviceStage.final_qc, STAGE_LABELS[DeviceStage.final_qc]),
    (DeviceStage.stock_in, STAGE_LABELS[DeviceStage.stock_in]),
    (DeviceStage.trc_production, "TRC Production"),
]
CHANGE_ENGINEER_STAGE_CODE = {stage: stage.value[:5] for stage, _ in CHANGE_ENGINEER_STAGES}


# Who can be handed a tag at each Change Engineer stage (2026-10-23): the
# built-in roles for that stage, plus any role (custom ones included) whose
# Module Permission row for the stage's page is switched on. Uses
# is_narrowly_scoped_to, not has_perm: has_perm treats an unconfigured module
# as allowed, which would let e.g. a cosmetic Masking role be given an L1/L2
# tag. Admin is always eligible.
_CE_STAGE_ROLES = {
    "l1": ("l1_engineer", "l2_engineer"), "l3": ("l3_engineer",),
    "qc_check": ("qc_inspector",), "cosmetic_received": ("cosmetic_manager",),
    "final_qc": ("qc_inspector", "cosmetic_manager"), "stock_in": ("inventory_manager",),
    "trc_production": ("inventory_manager",),
}
_CE_STAGE_MODULES = {
    "l1": ("repair_l1", "repair_l2"), "l3": ("repair_l3",), "qc_check": ("qc_check",),
    "cosmetic_received": ("cosmetic_received", "cosmetic_cleaning", "cosmetic_putty",
                          "cosmetic_dry_sanding", "cosmetic_masking", "cosmetic_painting",
                          "cosmetic_water_sanding", "cosmetic_completed"),
    "final_qc": ("cosmetic_finalqc",), "stock_in": ("stock",), "trc_production": ("production_manager",),
}


def _ce_user_eligible(user, stage_value: str) -> bool:
    from models.role_permissions import is_narrowly_scoped_to
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role == "admin" or role in _CE_STAGE_ROLES.get(stage_value, ()):
        return True
    mods = _CE_STAGE_MODULES.get(stage_value)
    return bool(mods) and is_narrowly_scoped_to(role, *mods)


@router.get("/api/change-engineer-users")
async def change_engineer_users(stage: str = "", db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(allowed)):
    """Production Manager's Change Engineer modal — "User Display Name"
    dropdown, sorted by display name. With ?stage= (the modal's target stage)
    only users who can work that stage are listed (2026-10-23); without it,
    every active user (old behaviour)."""
    rows = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )).scalars().all()
    if stage:
        rows = [u for u in rows if _ce_user_eligible(u, stage)]
    return JSONResponse([{"id": str(u.id), "name": u.full_name or u.username} for u in rows])


@router.post("/devices/change-engineer")
async def change_engineer(
    barcode: str = Form(...),
    assigned_user_id: str = Form(...),
    target_stage: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Production Manager's Change Engineer modal — search a tag from ANY
    stage (reuses /devices/api/search-tags, the same system-wide lookup the
    Search Tags modal uses), pick a user + target stage (default L1), and on
    submit: move the device to that stage, create a WorkOrder assigning it to
    that user, and notify them. Same WorkOrder/StageMovement shape as
    Cosmetic Received's bulk_assign (routers/cosmetic.py) and Production
    Manager's own bulk-assign-l1l2 above — no StockTransfer is written here
    since this isn't a location move, so WorkOrder.source_transfer_id is left
    unset (it's nullable, same as cosmetic.py's bulk_assign)."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode, Device.is_active == True)
    )).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Tag not found")

    try:
        user_uid = uuid.UUID(assigned_user_id)
    except Exception:
        raise HTTPException(400, "Invalid user")
    user = (await db.execute(
        select(User).where(User.id == user_uid, User.status == True)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    new_stage = None
    for stage, _ in CHANGE_ENGINEER_STAGES:
        if stage.value == target_stage:
            new_stage = stage
            break
    if not new_stage:
        raise HTTPException(400, "Invalid target stage")
    if not _ce_user_eligible(user, new_stage.value):
        # JSON (not an HTTPException page) so the modal can show the message.
        return JSONResponse({"ok": False, "error": (
            f"{user.full_name or user.username} can't work "
            f"{dict(CHANGE_ENGINEER_STAGES).get(new_stage, new_stage.value)} — pick a user for that stage")},
            status_code=400)

    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == prev_stage,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()

    device.current_stage = new_stage
    reset_repair_cycle_on_entry(device, prev_stage, new_stage)
    if new_stage == DeviceStage.l1:
        device.l1l2_status = "New"   # new engineer hasn't started (2026-10-23)
    device.updated_at = app_now()
    db.add(StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=new_stage,
        moved_by=current_user.username,
        notes=f"Changed engineer to {user.full_name or user.username} via Production Manager",
    ))

    # Same one-Stress-ID rule as bucket assignment (2026-10-17): reuse the
    # open, unassigned STRS- WorkID from L1/L2 Mark Complete if there is one.
    reused_wo = await reuse_open_stress_work_order(
        db, device.id, new_stage, user, created_by=current_user.username)
    # Previous engineer's WorkID is REASSIGNED, not completed (2026-10-23) —
    # except at L1/L2, L3/L4, Stress and Final QC, where a reassignment closes
    # it as COMPLETED (2026-10-27, one WorkID per visit; reused_wo is set there).
    if reused_wo:
        from services.work_order_close import close_open_work_orders
        await close_open_work_orders(db, device.id, keep_id=reused_wo.id)
    else:
        await reassign_open_work_orders(db, device.id)
    if reused_wo:
        work_id = reused_wo.work_id
    else:
        work_id = await _gen_work_id(db)
        db.add(WorkOrder(
            work_id=work_id, device_id=device.id, barcode=device.barcode,
            stage=CHANGE_ENGINEER_STAGE_CODE[new_stage],
            assigned_role=user.role.value if hasattr(user.role, "value") else str(user.role),
            assigned_user_id=user.id, assigned_username=user.username,
            assigned_name=user.full_name, status="pending",
            created_by=current_user.username,
        ))

    _label = f"{device.brand or ''} {device.model or ''}".strip()
    await create_notification(
        db, user_id=user.id,
        title="Device Assigned to You",
        message=(f"{device.barcode}" + (f" ({_label})" if _label else "")
                 + f" assigned to you via Change Engineer (WorkID: {work_id})."),
        notification_type="info",
        barcode=device.barcode, brand=device.brand, model=device.model,
        stage=new_stage.value,
    )

    await audit(db, action="change_engineer", user=current_user, table_name="devices",
                record_id=str(device.id),
                old_value={"stage": prev_stage.value if prev_stage else None},
                new_value={"stage": new_stage.value, "assigned_to": user.username, "work_id": work_id})

    await db.commit()
    return JSONResponse({"ok": True, "work_id": work_id})


@router.post("/scrap-products/{barcode}/move-to-inventory")
async def scrap_move_to_inventory(
    barcode: str, request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Scrap Products from Repair Line — 'Move to Inventory' (Batch 8 item 26):
    the device was mis-scrapped or found reusable on review, so send it back
    to Inventory Manager's stock_in stage."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode, Device.is_active == True)
    )).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    prev_stage = device.current_stage
    device.current_stage = DeviceStage.scrap_for_sale
    device.updated_at = app_now()
    db.add(StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.scrap_for_sale,
        moved_by=current_user.username, notes="Moved from Scrap to Scrap for Sale",
    ))
    await db.commit()
    return RedirectResponse(url="/trc-production?success=Moved+to+Scrap+for+Sale", status_code=302)


@router.post("/scrap-products/{barcode}/replace")
async def scrap_replace_with_another(
    barcode: str, request: Request,
    replacement_barcode: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Scrap Products from Repair Line — 'Replace With Another' (Batch 8 item
    26): record which tag number replaced this scrapped unit, on both sides,
    using the existing Device.replaced field (no new schema needed)."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode, Device.is_active == True)
    )).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    replacement = (await db.execute(
        select(Device).where(Device.barcode == replacement_barcode, Device.is_active == True)
    )).scalar_one_or_none()
    if not replacement:
        return RedirectResponse(
            url=f"/trc-production?error=Replacement+tag+{replacement_barcode}+not+found", status_code=302)

    device.replaced = f"Replaced by {replacement.barcode}"
    replacement.replaced = f"Replaced from {device.barcode}"
    await db.commit()
    return RedirectResponse(url="/trc-production?success=Replacement+recorded", status_code=302)


# ── Lot Line Items (Invoice Breakdown) ─────────────────────────────────────

@router.get("/lots/{lot_id}/line-items/export")
async def export_line_items_csv(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import csv as _csv, io as _io
    from fastapi.responses import StreamingResponse as SR
    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    result = await db.execute(
        select(LotLineItem).where(LotLineItem.lot_id == lot_id)
        .order_by(LotLineItem.sub_category, LotLineItem.brand, LotLineItem.model)
    )
    items = result.scalars().all()
    out = _io.StringIO()
    w = _csv.writer(out)
    w.writerow(["sub_category","brand","model","cpu","generation","ram_gb","has_ram","storage_gb","storage_type","has_storage","screen_size","grade","unit_price","qty","notes"])
    for item in items:
        w.writerow([
            item.sub_category, item.brand or "", item.model or "",
            item.cpu or "", item.generation or "",
            item.ram_gb or "", "yes" if item.has_ram else "no",
            item.storage_gb or "", item.storage_type or "", "yes" if item.has_storage else "no",
            item.screen_size or "", item.grade or "",
            item.unit_price, item.qty, item.notes or ""
        ])
    out.seek(0)
    fname = f"line_items_{lot.lot_number if lot else lot_id}.csv"
    return SR(
        _io.BytesIO(out.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@router.get("/lots/{lot_id}/next-tag", response_class=JSONResponse)
async def next_tag_number(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """System-generated Tag Number for a lot: <LOT_NUMBER>-NNN, where NNN is
    one past the highest existing numeric suffix among the lot's barcodes.
    Gap-safe and collision-checked against the whole devices table."""
    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")
    prefix = f"{lot.lot_number}-"
    rows = (await db.execute(
        select(Device.barcode).where(Device.barcode.like(prefix + "%"))
    )).scalars().all()
    highest = 0
    for bc in rows:
        suffix = bc[len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    n = highest + 1
    # Belt-and-braces: skip any value that exists on a device outside this lot
    while (await db.execute(
        select(Device.id).where(Device.barcode == f"{prefix}{n:03d}")
    )).scalar_one_or_none():
        n += 1
    return JSONResponse({"barcode": f"{prefix}{n:03d}"})


@router.get("/lots/{lot_id}/line-items", response_class=JSONResponse)
async def get_lot_line_items(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all line items for a lot as JSON."""
    result = await db.execute(
        select(LotLineItem)
        .where(LotLineItem.lot_id == lot_id)
        .order_by(LotLineItem.sub_category, LotLineItem.brand, LotLineItem.model)
    )
    items = result.scalars().all()
    return JSONResponse({"items": [
        {
            "id": str(item.id),
            "sub_category": item.sub_category,
            "brand": item.brand or "",
            "model": item.model or "",
            "cpu": item.cpu or "",
            "generation": item.generation or "",
            "ram_gb": item.ram_gb,
            "has_ram": item.has_ram,
            "storage_gb": item.storage_gb,
            "storage_type": item.storage_type or "",
            "has_storage": item.has_storage,
            "screen_size": item.screen_size or "",
            "grade": item.grade or "",
            "unit_price": float(item.unit_price),
            "qty": item.qty,
            "notes": item.notes or "",
            "label": _line_item_label(item),
        }
        for item in items
    ]})


def _line_item_label(item) -> str:
    """Generate a human-readable label for a line item."""
    parts = [item.sub_category]
    if item.brand:   parts.append(item.brand)
    if item.model:   parts.append(item.model)
    spec = []
    if item.ram_gb:
        spec.append(f"{item.ram_gb} GB RAM")
    elif item.has_ram is False:
        spec.append("No RAM")
    if item.storage_gb and item.storage_type:
        spec.append(f"{item.storage_gb} GB {item.storage_type}")
    elif item.storage_gb:
        spec.append(f"{item.storage_gb} GB Storage")
    elif item.has_storage is False:
        spec.append("No HDD/SSD")
    if spec:
        parts.append(f"[{', '.join(spec)}]")
    return " — ".join(parts)


@router.post("/lots/{lot_id}/line-items")
async def add_lot_line_item(
    lot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    body = await request.json()
    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        return JSONResponse({"ok": False, "error": "Lot not found"}, status_code=404)

    item = LotLineItem(
        lot_id       = lot.id,
        sub_category = body.get("sub_category", "Laptop").strip(),
        brand        = (body.get("brand") or "").strip() or None,
        model        = (body.get("model") or "").strip() or None,
        cpu          = (body.get("cpu") or "").strip() or None,
        generation   = (body.get("generation") or "").strip() or None,
        ram_gb       = int(body["ram_gb"]) if body.get("ram_gb") else None,
        has_ram      = body.get("has_ram", True),
        storage_gb   = int(body["storage_gb"]) if body.get("storage_gb") else None,
        storage_type = (body.get("storage_type") or "").strip() or None,
        has_storage  = body.get("has_storage", True),
        screen_size  = (body.get("screen_size") or "").strip() or None,
        grade        = (body.get("grade") or "").strip() or None,
        unit_price   = float(body["unit_price"]),
        qty          = int(body.get("qty", 1)),
        notes        = (body.get("notes") or "").strip() or None,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return JSONResponse({"ok": True, "id": str(item.id), "label": _line_item_label(item)})


@router.put("/lots/{lot_id}/line-items/{item_id}")
async def update_lot_line_item(
    lot_id: str,
    item_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    result = await db.execute(
        select(LotLineItem).where(LotLineItem.id == item_id, LotLineItem.lot_id == lot_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    body = await request.json()
    item.sub_category = body.get("sub_category", item.sub_category)
    item.brand        = (body.get("brand") or "").strip() or None
    item.model        = (body.get("model") or "").strip() or None
    item.cpu          = (body.get("cpu") or "").strip() or None
    item.generation   = (body.get("generation") or "").strip() or None
    item.ram_gb       = int(body["ram_gb"]) if body.get("ram_gb") else None
    item.has_ram      = body.get("has_ram", item.has_ram)
    item.storage_gb   = int(body["storage_gb"]) if body.get("storage_gb") else None
    item.storage_type = (body.get("storage_type") or "").strip() or None
    item.has_storage  = body.get("has_storage", item.has_storage)
    item.screen_size  = (body.get("screen_size") or "").strip() or None
    item.grade        = (body.get("grade") or "").strip() or None
    item.unit_price   = float(body["unit_price"])
    item.qty          = int(body.get("qty", item.qty))
    item.notes        = (body.get("notes") or "").strip() or None
    await db.commit()
    await db.refresh(item)
    return JSONResponse({"ok": True, "id": str(item.id), "label": _line_item_label(item)})


@router.delete("/lots/{lot_id}/line-items/{item_id}")
async def delete_lot_line_item(
    lot_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    result = await db.execute(
        select(LotLineItem).where(LotLineItem.id == item_id, LotLineItem.lot_id == lot_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    await db.delete(item)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/lots/{lot_id}/line-items/bulk-csv")
async def bulk_import_line_items(
    lot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Bulk import line items from CSV."""
    import csv as _csv, io as _io
    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        return JSONResponse({"ok": False, "error": "Lot not found"}, status_code=404)

    form = await request.form()
    file = form.get("file")
    if not file:
        return JSONResponse({"ok": False, "error": "No file"}, status_code=400)

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")

    reader = _csv.DictReader(_io.StringIO(text))
    saved, errors = 0, []
    for i, row in enumerate(reader, start=2):
        try:
            unit_price = float((row.get("unit_price") or "0").replace(",", ""))
            qty = int(row.get("qty") or 1)
            sub_cat = (row.get("sub_category") or "Laptop").strip()
            has_ram_val = (row.get("has_ram") or "yes").strip().lower()
            has_storage_val = (row.get("has_storage") or "yes").strip().lower()
            item = LotLineItem(
                lot_id       = lot.id,
                sub_category = sub_cat,
                brand        = (row.get("brand") or "").strip() or None,
                model        = (row.get("model") or "").strip() or None,
                cpu          = (row.get("cpu") or "").strip() or None,
                generation   = (row.get("generation") or "").strip() or None,
                ram_gb       = int(row["ram_gb"]) if row.get("ram_gb") else None,
                has_ram      = has_ram_val not in ("no", "false", "0"),
                storage_gb   = int(row["storage_gb"]) if row.get("storage_gb") else None,
                storage_type = (row.get("storage_type") or "").strip() or None,
                has_storage  = has_storage_val not in ("no", "false", "0"),
                screen_size  = (row.get("screen_size") or "").strip() or None,
                grade        = (row.get("grade") or "").strip() or None,
                unit_price   = unit_price,
                qty          = qty,
                notes        = (row.get("notes") or "").strip() or None,
            )
            db.add(item)
            saved += 1
        except Exception as e:
            errors.append(f"Row {i}: {e}")
    await db.commit()
    if errors:
        return RedirectResponse(
            url=f"/lots/{lot_id}?warning=Bulk+import+partial:+{saved}+saved,+{len(errors)}+error(s)",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/lots/{lot_id}?success=Bulk+import+complete:+{saved}+line+items+imported",
        status_code=302,
    )


# ── GRN Barcode Registration ────────────────────────────────────────────────

@router.get("/lots/{lot_id}/register", response_class=HTMLResponse)
async def grn_register_page(
    request: Request,
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """GRN barcode registration page — category-wise device entry for a lot."""
    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        return RedirectResponse(url="/lots?error=Lot+not+found", status_code=302)

    # Devices already registered in this lot (at grn or iqc stage)
    result = await db.execute(
        select(Device).where(Device.lot_id == lot.id).order_by(Device.created_at.desc())
    )
    devices = result.scalars().all()

    # Group by sub_category
    from collections import defaultdict
    by_category = defaultdict(list)
    for d in devices:
        by_category[d.sub_category or "Other"].append(d)

    return templates.TemplateResponse("lots/grn_register.html", {
        "request":     request,
        "lot":         lot,
        "devices":     devices,
        "by_category": dict(by_category),
        "total_registered": len(devices),
        "current_user": current_user,
    })


@router.post("/lots/{lot_id}/register-device")
async def register_device_barcode(
    request: Request,
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AJAX: register a single device barcode into a lot at GRN stage."""
    import json as _json
    body = await request.json()

    barcode    = (body.get("barcode") or "").strip()
    brand      = (body.get("brand") or "").strip()
    model_name = (body.get("model") or "").strip()
    serial_no  = (body.get("serial_no") or "").strip()
    sub_cat    = (body.get("sub_category") or "Laptop").strip()
    device_price = body.get("device_price")

    if not barcode:
        return JSONResponse({"ok": False, "error": "Barcode is required"}, status_code=400)

    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        return JSONResponse({"ok": False, "error": "Lot not found"}, status_code=404)

    # Check duplicate barcode — show lot_number not UUID. Case-folded: a tag
    # scanned or typed with different capitalisation than however it was first
    # entered used to pass this check clean and register as a second, fully
    # independent device for the same physical unit. The barcode column's
    # UNIQUE constraint is case-sensitive at the database level, so nothing
    # there caught it either.
    dup_barcode = (await db.execute(
        select(Device, Lot.lot_number).join(Lot, Device.lot_id == Lot.id)
        .where(func.upper(Device.barcode) == barcode.upper())
    )).first()
    if dup_barcode:
        _dev, _lot_num = dup_barcode
        return JSONResponse({
            "ok": False,
            "error": f"Barcode {barcode} already registered in lot {_lot_num}"
        }, status_code=409)

    # Check duplicate serial_no (if provided)
    if serial_no:
        dup_serial = (await db.execute(
            select(Device, Lot.lot_number).join(Lot, Device.lot_id == Lot.id)
            .where(Device.serial_no == serial_no, Device.is_active == True)
        )).first()
        if dup_serial:
            _dev, _lot_num = dup_serial
            return JSONResponse({
                "ok": False,
                "error": f"Serial {serial_no} already registered as barcode {_dev.barcode} in lot {_lot_num}"
            }, status_code=409)

    device = Device(
        barcode       = barcode,
        lot_id        = lot.id,
        brand         = brand or None,
        model         = model_name or None,
        serial_no     = serial_no or None,
        sub_category  = sub_cat,
        current_stage = DeviceStage.grn,
        grn_number    = lot.grn_system_number or str(lot.grn_number_new or ""),
        device_price  = float(device_price) if device_price else None,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)

    return JSONResponse({
        "ok":          True,
        "barcode":     device.barcode,
        "brand":       device.brand or "",
        "model":       device.model or "",
        "serial_no":   device.serial_no or "",
        "sub_category": device.sub_category or "",
        "device_price": str(device.device_price or ""),
        "registered_at": device.created_at.strftime("%d-%m-%Y %H:%M"),
    })


@router.delete("/lots/{lot_id}/register-device/{barcode}")
async def delete_registered_device(
    request: Request,
    lot_id: str,
    barcode: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AJAX: remove a device from GRN registration (only if still at grn stage)."""
    device = (await db.execute(
        select(Device).where(Device.barcode == barcode, Device.lot_id == lot_id)
    )).scalar_one_or_none()

    if not device:
        return JSONResponse({"ok": False, "error": "Device not found"}, status_code=404)
    if device.current_stage != DeviceStage.grn:
        return JSONResponse({"ok": False, "error": f"Cannot remove — device already moved to {device.current_stage}"}, status_code=400)

    device.is_active = False
    device.deleted_at = datetime.now(timezone.utc)
    await audit(db, action="DEVICE_SOFT_DELETED", user=current_user,
                table_name="devices", record_id=str(device.id),
                old_value={"barcode": device.barcode, "is_active": True},
                new_value={"barcode": device.barcode, "is_active": False, "deleted_at": device.deleted_at.isoformat()},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/lots/{lot_id}/register-bulk-csv")
async def register_bulk_csv(
    request: Request,
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk register devices from uploaded CSV/Excel data."""
    import csv as _csv, io as _io
    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        return JSONResponse({"ok": False, "error": "Lot not found"}, status_code=404)

    form = await request.form()
    file = form.get("file")
    if not file:
        return JSONResponse({"ok": False, "error": "No file uploaded"}, status_code=400)

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")  # handle BOM
    except Exception:
        text_content = content.decode("latin-1")

    reader = _csv.DictReader(_io.StringIO(text_content))
    saved, skipped, errors = 0, 0, []

    for row in reader:
        barcode = (row.get("barcode") or row.get("Barcode") or row.get("BARCODE") or "").strip()
        if not barcode:
            continue

        # Same case-folding as the single-device path above — see its comment.
        existing = (await db.execute(
            select(Device).where(func.upper(Device.barcode) == barcode.upper())
        )).scalar_one_or_none()
        if existing:
            skipped += 1
            continue

        sub_cat = (row.get("sub_category") or row.get("Category") or row.get("category") or "Laptop").strip()
        try:
            price_raw = row.get("device_price") or row.get("price") or row.get("Price") or ""
            price = float(price_raw.replace(",", "")) if price_raw.strip() else None
        except Exception:
            price = None

        device = Device(
            barcode       = barcode,
            lot_id        = lot.id,
            brand         = (row.get("brand") or row.get("Brand") or "").strip() or None,
            model         = (row.get("model") or row.get("Model") or "").strip() or None,
            serial_no     = (row.get("serial_no") or row.get("Serial") or row.get("serial") or "").strip() or None,
            sub_category  = sub_cat,
            current_stage = DeviceStage.grn,
            grn_number    = lot.grn_system_number or str(lot.grn_number_new or ""),
            device_price  = price,
        )
        db.add(device)
        saved += 1

    await db.commit()
    return JSONResponse({"ok": True, "saved": saved, "skipped": skipped, "errors": errors})


@router.post("/lots/{lot_id}/advance-grn-to-iqc")
async def advance_grn_to_iqc(
    request: Request,
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move all GRN-stage devices in this lot forward to IQC."""
    from sqlalchemy import update as sa_update
    result = await db.execute(
        select(Device).where(Device.lot_id == lot_id, Device.current_stage == DeviceStage.grn)
    )
    devices = result.scalars().all()
    if not devices:
        return JSONResponse({"ok": False, "error": "No devices at GRN stage in this lot"})

    for d in devices:
        d.current_stage = DeviceStage.iqc
    await db.commit()
    return JSONResponse({"ok": True, "moved": len(devices)})
