"""
Parts GRN Router — Goods Receipt Note for spare parts procurement
"""
import os
import json
import secrets
from decimal import Decimal, InvalidOperation
from datetime import datetime
from templates_config import templates
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, File, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.user import User, UserRole
from models.parts_grn import PartsGRN, PartsGRNLineItem
from models.spare_parts import SparePart
from utils.master_data import master_values
from auth.dependencies import get_current_user, require_roles, verify_csrf
from services.audit_engine import audit
from config import UPLOADS_DIR

router = APIRouter(prefix="/parts-grn", tags=["parts_grn"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.spare_parts_manager)

PARTS_GRN_DIR = os.path.join(UPLOADS_DIR, "parts_grn")
os.makedirs(PARTS_GRN_DIR, exist_ok=True)

MAIN_CATEGORIES = ["Hardware", "Accessories", "Consumables", "Other"]


def _f(v: str | None) -> str | None:
    return v.strip() if v and v.strip() else None


def _i(v) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _d(v) -> Decimal | None:
    if v is None:
        return None
    try:
        s = str(v).strip()
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


def _parse_date(s: str | None) -> datetime | None:
    if not s or not s.strip():
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except ValueError:
        return None


async def _save_upload(upload: UploadFile | None, prefix: str) -> str | None:
    if not upload or not upload.filename:
        return None
    data = await upload.read()
    if not data:
        return None
    safe = upload.filename.replace("/", "_").replace("\\", "_")
    stored = f"{prefix}_{safe}"
    with open(os.path.join(PARTS_GRN_DIR, stored), "wb") as f:
        f.write(data)
    return stored


async def _next_grn_number(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(PartsGRN.id)))
    count = (result.scalar() or 0) + 1
    return f"{count:012d}"


def _new_part_id() -> str:
    return f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"


@router.get("", response_class=HTMLResponse)
async def grn_list(request: Request, db: AsyncSession = Depends(get_db),
                   current_user: User = Depends(allowed),
                   error: str = "", success: str = ""):
    result = await db.execute(
        select(PartsGRN).where(PartsGRN.is_trashed == False)
        .order_by(PartsGRN.created_at.desc())
    )
    grns = result.scalars().all()
    from utils.user_lookup import added_by_display_map   # Added By shows the full name (2026-11-04)
    return templates.TemplateResponse("spare_parts/parts_grn_list.html", {
        "request": request, "current_user": current_user, "grns": grns,
        "error": error, "success": success, "added_by": await added_by_display_map(db),
    })


@router.post("/{grn_id}/delete")
async def grn_delete(grn_id: str, request: Request,
                     db: AsyncSession = Depends(get_db),
                     current_user: User = Depends(allowed)):
    import uuid as _uuid
    try:
        gid = _uuid.UUID(grn_id)
    except ValueError:
        raise HTTPException(404, "GRN not found")
    grn = (await db.execute(select(PartsGRN).where(PartsGRN.id == gid))).scalar_one_or_none()
    if not grn:
        raise HTTPException(404, "GRN not found")
    grn.is_trashed = True
    grn.trashed_at = app_now()
    await audit(db, action="PARTS_GRN_DELETE", user=current_user,
                table_name="parts_grn", record_id=str(grn.id))
    await db.commit()
    return RedirectResponse(url="/parts-grn?success=GRN+deleted", status_code=302)


@router.get("/new", response_class=HTMLResponse)
async def grn_new_form(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(allowed)):
    grn_number = await _next_grn_number(db)
    return templates.TemplateResponse("spare_parts/parts_grn_form.html", {
        "request": request, "current_user": current_user,
        "grn": None, "grn_number": grn_number, "line_items": [],
        "main_categories": MAIN_CATEGORIES, "categories": await master_values(db, "iqc_part_category"),
        "part_names": await master_values(db, "part_category"),
        "part_brands": await master_values(db, "spare_part_brand"),
        "state_options": await master_values(db, "dealer_state"),
        "gst_cfg": await _gst_cfg_parts(db),
    })


async def _gst_cfg_parts(db) -> dict:
    from services.grn_tax import gst_rates, company_states
    intra, inter = await gst_rates(db)
    return {"intra": float(intra), "inter": float(inter), "states": await company_states(db)}


@router.post("/new", response_class=HTMLResponse)
async def grn_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    po_number: str = Form(default=""),
    po_date: str = Form(default=""),
    invoice_number: str = Form(default=""),
    invoice_date: str = Form(default=""),
    eway_bill_number: str = Form(default=""),
    eway_bill_date: str = Form(default=""),
    vehicle_number: str = Form(default=""),
    date_received: str = Form(default=""),
    vendor_name: str = Form(default=""),
    invoice_value: str = Form(default=""),
    sgst: str = Form(default=""),
    cgst: str = Form(default=""),
    igst: str = Form(default=""),
    tax_amount: str = Form(default=""),
    total_po_qty: str = Form(default=""),
    total_invoice_qty: str = Form(default=""),
    total_physical_qty: str = Form(default=""),
    total_amount_invoice: str = Form(default=""),
    vendor_state: str | None = Form(default=None),
    line_items_json: str = Form(default="[]"),
    po_file: UploadFile = File(default=None),
    invoice_file: UploadFile = File(default=None),
    eway_bill_file: UploadFile = File(default=None),
    vehicle_seal_file: UploadFile = File(default=None),
    vehicle_image_file: UploadFile = File(default=None),
):
    # One crate = one part (2026-10-31): refuse the GRN before anything is
    # saved when a line puts a part into a crate that holds another part
    # (in Part Master, or on an earlier line of this same GRN).
    # Pricing Add (2026-11-03): invoice amounts and line prices are ignored for a
    # role that may not set prices (Invoice Price is then not required either).
    from utils.price_export import can_add_prices as _cap
    _px = _cap(current_user, "spare_parts_purchase")
    if not _px:
        invoice_value = sgst = cgst = igst = tax_amount = total_amount_invoice = ""
    from services.parts_stock import crate_conflict, conflict_text, group_key as _gk, norm as _norm
    try:
        _items = json.loads(line_items_json or "[]")
    except json.JSONDecodeError:
        _items = []
    _seen: dict = {}
    for _it in _items:
        _nm = _f(_it.get("part_name")) or _f(_it.get("item_name")) or _f(_it.get("product_description")) or "GRN Part"
        _cr = _f(_it.get("crate_number"))
        if not _cr:
            continue
        _g = _gk(_nm, _f(_it.get("part_brand")), _f(_it.get("part_model")))
        _other = await crate_conflict(db, _nm, _f(_it.get("part_brand")), _f(_it.get("part_model")), _cr)
        _msg = conflict_text(_cr, _other) if _other is not None else None
        if _msg is None and _seen.get(_norm(_cr), _g) != _g:
            _msg = f"Crate {_cr} is used for two different parts in this GRN — one crate holds one part"
        if _msg:
            from urllib.parse import quote_plus
            return RedirectResponse(url="/parts-grn/new?error=" + quote_plus("GRN not saved: " + _msg), status_code=303)
        _seen[_norm(_cr)] = _g

    # New Section 1 (2026-10-31, sends vendor_state): required fields, and
    # Tax / Taxable computed here from the GST-inclusive Invoice Price — the
    # posted CGST / SGST / IGST / Tax are ignored. Total Amount Invoice =
    # Invoice Price (one amount).
    _tax_fields = None
    if vendor_state is not None:
        _req = [("GRN Date", date_received), ("Vendor Name", vendor_name), ("Vendor State", vendor_state),
                ("PO Number", po_number), ("PO Date", po_date), ("Invoice Number", invoice_number),
                ("Invoice Date", invoice_date),   # E-Way Bill Number / Date optional (2026-11-03)
                ("Vehicle Number", vehicle_number),
                *([("Invoice Price", invoice_value)] if _px else []), ("PO Quantity", total_po_qty),
                ("Invoice Quantity", total_invoice_qty), ("Physical Quantity", total_physical_qty)]
        from routers.grn import eway_missing
        _miss = [lbl for lbl, v in _req if not (v or "").strip()] + eway_missing(
            invoice_value, eway_bill_number, eway_bill_date)
        if _miss:
            from urllib.parse import quote_plus
            return RedirectResponse(url="/parts-grn/new?error=" + quote_plus("GRN not saved — fill in: " + ", ".join(_miss)),
                                    status_code=303)
        from services.grn_tax import compute_grn_tax
        _gt, _rate, _tax, _taxable = await compute_grn_tax(
            db, getattr(current_user, "entity", None), vendor_state, _d(invoice_value))
        if _tax is not None:
            _half = (_tax / 2).quantize(Decimal("0.01"))
            _tax_fields = {"tax_amount": _tax,
                           "cgst": _half if _gt == "intra" else None,
                           "sgst": (_tax - _half) if _gt == "intra" else None,
                           "igst": _tax if _gt == "inter" else None}

    grn_number = await _next_grn_number(db)
    pfx = grn_number

    grn = PartsGRN(
        grn_number=grn_number,
        po_number=_f(po_number),
        po_date=_parse_date(po_date),
        po_file=await _save_upload(po_file, f"{pfx}_po"),
        invoice_number=_f(invoice_number),
        invoice_date=_parse_date(invoice_date),
        invoice_file=await _save_upload(invoice_file, f"{pfx}_inv"),
        eway_bill_number=_f(eway_bill_number),
        eway_bill_date=_parse_date(eway_bill_date),
        eway_bill_file=await _save_upload(eway_bill_file, f"{pfx}_eway"),
        vehicle_number=_f(vehicle_number),
        vehicle_seal_file=await _save_upload(vehicle_seal_file, f"{pfx}_seal"),
        vehicle_image_file=await _save_upload(vehicle_image_file, f"{pfx}_veh"),
        date_received=_parse_date(date_received),
        vendor_name=_f(vendor_name),
        invoice_value=_d(invoice_value),
        sgst=_d(sgst),
        cgst=_d(cgst),
        igst=_d(igst),
        tax_amount=_d(tax_amount),
        total_po_qty=_i(total_po_qty),
        total_invoice_qty=_i(total_invoice_qty),
        total_physical_qty=_i(total_physical_qty),
        total_amount_invoice=_d(total_amount_invoice),
        created_by=current_user.username,
    )
    if vendor_state is not None:
        grn.vendor_state = vendor_state.strip() or None
        grn.total_amount_invoice = _d(invoice_value)
        if _tax_fields:
            for _k, _v in _tax_fields.items():
                setattr(grn, _k, _v)
    db.add(grn)
    await db.flush()

    try:
        items = json.loads(line_items_json or "[]")
    except json.JSONDecodeError:
        items = []

    from services.parts_stock import find_row, apply_group_price, latest_grn_price
    for item in items:
        # Crate rows (2026-10-31): the received Physical Qty goes into the Part
        # Master row for Part Name + Make + Model + New + this Crate — added to
        # it when it exists (Matched), a new row otherwise (Not-Matched). The
        # line's unit price then becomes the price of every crate row of that
        # Part Name + Make + Model, New and Harvest.
        sp_name = _f(item.get("part_name")) or _f(item.get("item_name")) or _f(item.get("product_description")) or "GRN Part"
        sp_make = _f(item.get("part_brand"))
        sp_model = _f(item.get("part_model"))
        sp_crate = _f(item.get("crate_number"))
        sp_qty = _i(item.get("physical_qty")) or _i(item.get("invoice_qty")) or 0
        sp_price = _d(item.get("price")) if _px else None   # Pricing Add (2026-11-03)
        sp_min_alert = _i(item.get("min_stock_alert"))
        row = await find_row(db, sp_name, sp_make, sp_model, "new", sp_crate)
        if row is not None:
            part_id = row.part_code
        else:
            part_id = item.get("part_id") or _new_part_id()
            while (await db.execute(select(SparePart.id).where(SparePart.part_code == part_id))).first():
                part_id = _new_part_id()
        db.add(PartsGRNLineItem(
            grn_id=grn.id,
            part_id=part_id,
            lot_number=_f(item.get("lot_number")),
            po_number=_f(item.get("po_number")) or grn.po_number,
            grn_number=grn_number,
            vendor_name=_f(item.get("vendor_name")) or grn.vendor_name,
            invoice_number=_f(item.get("invoice_number")) or grn.invoice_number,
            product_description=_f(item.get("product_description")),
            item_name=_f(item.get("item_name")),
            part_brand=sp_make,
            part_model=sp_model,
            part_name=_f(item.get("part_name")),
            invoice_qty=_i(item.get("invoice_qty")),
            physical_qty=_i(item.get("physical_qty")),
            price=sp_price,
            main_category=_f(item.get("main_category")),
            category=_f(item.get("category")),
            vehicle_number=_f(item.get("vehicle_number")) or grn.vehicle_number,
            invoice_ref=_f(item.get("invoice_ref")),
            remarks=_f(item.get("remarks")),
            is_harvest=False,
            crate_number=sp_crate,
        ))
        if row is not None:
            row.qty_in_stock = int(row.qty_in_stock or 0) + sp_qty
            if sp_min_alert is not None:
                row.min_stock_alert = sp_min_alert
        else:
            # A line with no price: the new crate row takes the part's latest GRN price.
            _p = sp_price if (sp_price is not None and sp_price > 0) else await latest_grn_price(db, sp_name, sp_make, sp_model)
            db.add(SparePart(
                part_code=part_id,
                name=sp_name,
                category=_f(item.get("category")) or "Other",
                unit_price=float(_p) if _p is not None else 0.0,
                qty_in_stock=sp_qty,
                min_stock_alert=sp_min_alert if sp_min_alert is not None else 5,
                supplier=_f(item.get("vendor_name")) or grn.vendor_name,
                notes=_f(item.get("product_description")),
                source="new",
                make=sp_make,
                model=sp_model,
                crate_number=sp_crate,
            ))
        await db.flush()
        if sp_price is not None:
            await apply_group_price(db, sp_name, sp_make, sp_model, sp_price)

    await db.commit()
    await audit(db, action="PARTS_GRN_CREATE", user=current_user,
                table_name="parts_grn", record_id=str(grn.id))
    return RedirectResponse(f"/parts-grn/{grn.id}", status_code=303)


@router.get("/{grn_id}", response_class=HTMLResponse)
async def grn_detail(grn_id: str, request: Request, db: AsyncSession = Depends(get_db),
                     current_user: User = Depends(allowed)):
    try:
        import uuid as _uuid
        uid = _uuid.UUID(grn_id)
    except ValueError:
        raise HTTPException(404, "GRN not found")
    result = await db.execute(select(PartsGRN).where(PartsGRN.id == uid))
    grn = result.scalar_one_or_none()
    if not grn:
        raise HTTPException(404, "GRN not found")
    items_res = await db.execute(
        select(PartsGRNLineItem).where(PartsGRNLineItem.grn_id == grn.id)
        .order_by(PartsGRNLineItem.created_at)
    )
    line_items = items_res.scalars().all()
    # Parts GRN Detail page (2026-11-02): read-only, laid out like the device
    # GRN Detail page, with a Parts tab. ?form=1 keeps the old form view.
    if request.query_params.get("form") != "1":
        from models.user import User as _U
        added_by = (await db.execute(select(_U.full_name).where(_U.username == grn.created_by))).scalar() \
            if grn.created_by else None
        return templates.TemplateResponse("spare_parts/parts_grn_detail.html", {
            "request": request, "current_user": current_user, "grn": grn,
            "line_items": line_items, "added_by": added_by,
            # Edit GRN / Edit part popups (2026-11-04)
            "state_options": await master_values(db, "dealer_state"),
            "part_names": await master_values(db, "part_category"),
            "part_brands": await master_values(db, "spare_part_brand"),
            "categories": await master_values(db, "iqc_part_category"),
        })
    return templates.TemplateResponse("spare_parts/parts_grn_form.html", {
        "request": request, "current_user": current_user,
        "grn": grn, "grn_number": grn.grn_number, "line_items": line_items,
        "main_categories": MAIN_CATEGORIES, "categories": await master_values(db, "iqc_part_category"),
        # The Add Line Item modal is hidden on this read-only detail view
        # (`ro = grn is not none` in the template), so these are unused here —
        # included anyway so the shared template never renders with one
        # branch wired and the other not.
        "part_names": await master_values(db, "part_category"),
        "part_brands": await master_values(db, "spare_part_brand"),
    })


async def _grn_or_404(db, grn_id):
    import uuid as _uuid
    try:
        uid = _uuid.UUID(grn_id)
    except ValueError:
        raise HTTPException(404, "GRN not found")
    grn = (await db.execute(select(PartsGRN).where(PartsGRN.id == uid))).scalar_one_or_none()
    if not grn:
        raise HTTPException(404, "GRN not found")
    return grn


def _back(grn_id, ok=None, err=None):
    from urllib.parse import quote_plus
    q = ("success=" + quote_plus(ok)) if ok else ("error=" + quote_plus(err or ""))
    return RedirectResponse(f"/parts-grn/{grn_id}?{q}", status_code=303)


@router.post("/{grn_id}/edit")
async def grn_edit(grn_id: str, request: Request, db: AsyncSession = Depends(get_db),
                   current_user: User = Depends(allowed),
                   date_received: str = Form(""), vendor_name: str = Form(""), vendor_state: str = Form(""),
                   po_number: str = Form(""), po_date: str = Form(""),
                   invoice_number: str = Form(""), invoice_date: str = Form(""),
                   eway_bill_number: str = Form(""), eway_bill_date: str = Form(""),
                   vehicle_number: str = Form(""), invoice_value: str = Form(""),
                   total_po_qty: str = Form(""), total_invoice_qty: str = Form(""),
                   total_physical_qty: str = Form("")):
    """Edit a Parts GRN's details from its Detail page (2026-11-04). Files stay
    as uploaded. Invoice Price needs Pricing Add; Tax / Taxable are worked out
    again from Invoice Price + Vendor State, as on Add GRN."""
    grn = await _grn_or_404(db, grn_id)
    from routers.grn import eway_missing
    from utils.price_export import price_unchanged
    if price_unchanged(current_user, "spare_parts_purchase", invoice_value):
        invoice_value = "" if grn.invoice_value is None else str(grn.invoice_value)
    req = [("GRN Date", date_received), ("Vendor Name", vendor_name), ("Vendor State", vendor_state),
           ("PO Number", po_number), ("Invoice Number", invoice_number), ("Vehicle Number", vehicle_number)]
    miss = [lbl for lbl, v in req if not (v or "").strip()] + eway_missing(invoice_value, eway_bill_number, eway_bill_date)
    if miss:
        return _back(grn.id, err="GRN not saved — fill in: " + ", ".join(miss))
    before = {k: str(getattr(grn, k)) if getattr(grn, k) is not None else None for k in (
        "date_received", "vendor_name", "vendor_state", "po_number", "po_date", "invoice_number", "invoice_date",
        "eway_bill_number", "eway_bill_date", "vehicle_number", "invoice_value", "total_po_qty",
        "total_invoice_qty", "total_physical_qty")}
    grn.date_received = _parse_date(date_received)
    grn.vendor_name = _f(vendor_name)
    grn.vendor_state = _f(vendor_state)
    grn.po_number, grn.po_date = _f(po_number), _parse_date(po_date)
    grn.invoice_number, grn.invoice_date = _f(invoice_number), _parse_date(invoice_date)
    grn.eway_bill_number, grn.eway_bill_date = _f(eway_bill_number), _parse_date(eway_bill_date)
    grn.vehicle_number = _f(vehicle_number)
    grn.invoice_value = _d(invoice_value)
    grn.total_amount_invoice = grn.invoice_value
    grn.total_po_qty, grn.total_invoice_qty = _i(total_po_qty), _i(total_invoice_qty)
    grn.total_physical_qty = _i(total_physical_qty)
    from services.grn_tax import compute_grn_tax
    gt, _rate, tax, _taxable = await compute_grn_tax(db, getattr(current_user, "entity", None), grn.vendor_state, grn.invoice_value)
    if tax is not None:
        half = (tax / 2).quantize(Decimal("0.01"))
        grn.tax_amount = tax
        grn.cgst, grn.sgst = (half, tax - half) if gt == "intra" else (None, None)
        grn.igst = tax if gt == "inter" else None
    elif grn.invoice_value is None:
        grn.tax_amount = grn.cgst = grn.sgst = grn.igst = None
    await audit(db, action="PARTS_GRN_EDITED", user=current_user, table_name="parts_grn",
                record_id=str(grn.id), old_value=before,
                new_value={k: str(getattr(grn, k)) if getattr(grn, k) is not None else None for k in before},
                request=request)
    await db.commit()
    return _back(grn.id, ok="GRN details saved.")


@router.post("/{grn_id}/items/{item_id}/edit")
async def grn_item_edit(grn_id: str, item_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed),
                        part_name: str = Form(""), part_brand: str = Form(""), part_model: str = Form(""),
                        category: str = Form(""), crate_number: str = Form(""),
                        invoice_qty: str = Form(""), physical_qty: str = Form(""),
                        price: str = Form(""), remarks: str = Form("")):
    """Edit one part line of a Parts GRN (2026-11-04). Stock follows the change:
    a different Physical Qty moves the difference in / out of the line's crate
    row; a different Part Name / Make / Model / Crate takes the old qty out of
    the old crate row and puts the new qty into the matching crate row (a new
    row when there is none). Refused when that would take out more than the old
    row still has in stock (already handed over / sold)."""
    import uuid as _uuid
    grn = await _grn_or_404(db, grn_id)
    try:
        iid = _uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(404, "Part line not found")
    li = (await db.execute(select(PartsGRNLineItem).where(
        PartsGRNLineItem.id == iid, PartsGRNLineItem.grn_id == grn.id))).scalar_one_or_none()
    if li is None:
        raise HTTPException(404, "Part line not found")
    from services.parts_stock import find_row, live_map, apply_group_price, row_key
    from utils.price_export import price_unchanged
    new_name = _f(part_name)
    if not new_name:
        return _back(grn.id, err="Part not saved — Part Name is required.")
    new_make, new_model, new_crate = _f(part_brand), _f(part_model), _f(crate_number)
    new_phys = _i(physical_qty)
    if new_phys is None or new_phys < 0:
        return _back(grn.id, err="Part not saved — Physical Qty must be 0 or more.")
    source = "harvest" if li.is_harvest else "new"
    old_name = li.part_name or li.item_name or li.product_description or "GRN Part"
    old_qty = int(li.physical_qty if li.physical_qty is not None else (li.invoice_qty or 0))
    old_row = (await db.execute(select(SparePart).where(SparePart.part_code == li.part_id,
                                                         SparePart.is_trashed.isnot(True)))).scalars().first()
    if old_row is None:
        old_row = await find_row(db, old_name, li.part_brand, li.part_model, source, li.crate_number)
    same = row_key(old_name, li.part_brand, li.part_model, source, li.crate_number) == \
        row_key(new_name, new_make, new_model, source, new_crate)
    take_out = (old_qty - new_phys) if same else old_qty
    if old_row is not None and take_out > 0:
        avail = (await live_map(db, [old_row])).get(str(old_row.id), 0)
        if take_out > avail:
            return _back(grn.id, err=(f"Part not saved — crate row {old_row.part_code} has only {avail} in stock, "
                                      f"so {take_out} cannot be taken out (already handed over or sold)."))
    before = {"part_id": li.part_id, "part_name": li.part_name, "make": li.part_brand, "model": li.part_model,
              "category": li.category, "crate": li.crate_number, "invoice_qty": li.invoice_qty,
              "physical_qty": li.physical_qty, "price": str(li.price) if li.price is not None else None,
              "remarks": li.remarks}
    if same:
        if old_row is not None:
            old_row.qty_in_stock = int(old_row.qty_in_stock or 0) + (new_phys - old_qty)
    else:
        if old_row is not None:
            old_row.qty_in_stock = int(old_row.qty_in_stock or 0) - old_qty
        target = await find_row(db, new_name, new_make, new_model, source, new_crate)
        if target is not None:
            target.qty_in_stock = int(target.qty_in_stock or 0) + new_phys
            li.part_id = target.part_code
        else:
            code = _new_part_id()
            while (await db.execute(select(SparePart.id).where(SparePart.part_code == code))).first():
                code = _new_part_id()
            unit = li.price if li.price is not None else (old_row.unit_price if old_row is not None else 0)
            db.add(SparePart(part_code=code, name=new_name, category=_f(category) or "Other",
                             unit_price=float(unit or 0), qty_in_stock=new_phys,
                             min_stock_alert=old_row.min_stock_alert if old_row is not None else 5,
                             supplier=grn.vendor_name, source=source, make=new_make, model=new_model,
                             crate_number=new_crate))
            li.part_id = code
    li.part_name, li.part_brand, li.part_model = new_name, new_make, new_model
    li.category, li.crate_number = _f(category), new_crate
    li.invoice_qty, li.physical_qty = _i(invoice_qty), new_phys
    li.remarks = _f(remarks)
    if not price_unchanged(current_user, "spare_parts_purchase", price):
        li.price = _d(price)
    await db.flush()
    if li.price is not None and before["price"] != str(li.price):
        await apply_group_price(db, new_name, new_make, new_model, li.price)
    await audit(db, action="PARTS_GRN_ITEM_EDITED", user=current_user, table_name="parts_grn_line_items",
                record_id=str(li.id), old_value=before,
                new_value={"part_id": li.part_id, "part_name": li.part_name, "make": li.part_brand,
                           "model": li.part_model, "category": li.category, "crate": li.crate_number,
                           "invoice_qty": li.invoice_qty, "physical_qty": li.physical_qty,
                           "price": str(li.price) if li.price is not None else None, "remarks": li.remarks},
                request=request)
    await db.commit()
    return _back(grn.id, ok=f"Part line {li.part_id} saved.")


@router.post("/harvest", response_class=JSONResponse)
async def harvest_part(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    lot_number: str = Form(default=""),
    product_description: str = Form(default=""),
    part_name: str = Form(default=""),
    part_brand: str = Form(default=""),
    part_model: str = Form(default=""),
    physical_qty: str = Form(default="1"),
    price: str = Form(default=""),
    main_category: str = Form(default=""),
    category: str = Form(default=""),
    vehicle_number: str = Form(default=""),
    invoice_ref: str = Form(default=""),
    remarks: str = Form(default=""),
    min_stock_alert: str = Form(default="5"),
    crate_number: str = Form(default=""),
):
    # Pricing Add (2026-11-03): entered price ignored without it (GRN price still applies).
    from utils.price_export import can_add_prices as _cap
    if not _cap(current_user, "spare_parts"):
        price = ""
    from services.parts_stock import find_row, lowest_grn_price, crate_conflict, conflict_text
    _other = await crate_conflict(db, _f(part_name) or _f(product_description) or "Harvest Part",
                                  _f(part_brand), _f(part_model), crate_number)
    if _other is not None:
        return JSONResponse({"ok": False, "detail": conflict_text(crate_number.strip(), _other)}, status_code=400)
    grn_number = await _next_grn_number(db)
    grn = PartsGRN(
        grn_number=grn_number,
        po_number="Harvest",
        vendor_name="Internal",
        invoice_number="Internal",
        created_by=current_user.username,
    )
    db.add(grn)
    await db.flush()

    qty = _i(physical_qty) or 1
    sp_name = _f(part_name) or _f(product_description) or "Harvest Part"
    sp_crate = _f(crate_number)
    # Crate rows (2026-10-31): add to the Harvest row for Part Name + Make +
    # Model + this Crate when it exists, else create it. Harvest price = the
    # LOWEST Parts GRN unit price of the same Part Name + Make + Model
    # (2026-11-02; was the latest); the entered price is used only when that
    # part has never come in on a GRN.
    row = await find_row(db, sp_name, _f(part_brand), _f(part_model), "harvest", sp_crate)
    grn_price = await lowest_grn_price(db, sp_name, _f(part_brand), _f(part_model))
    sp_price = grn_price if grn_price is not None else _d(price)
    part_id = row.part_code if row is not None else _new_part_id()
    db.add(PartsGRNLineItem(
        grn_id=grn.id,
        part_id=part_id,
        grn_number="Not Available",
        lot_number=_f(lot_number),
        po_number="Harvest",
        vendor_name="Internal",
        invoice_number="Internal",
        product_description=_f(product_description),
        part_name=_f(part_name),
        part_brand=_f(part_brand),
        part_model=_f(part_model),
        physical_qty=qty,
        invoice_qty=qty,
        price=_d(price),
        main_category=_f(main_category),
        category=_f(category),
        vehicle_number=_f(vehicle_number),
        invoice_ref=_f(invoice_ref),
        remarks=_f(remarks),
        is_harvest=True,
        crate_number=sp_crate,
    ))
    if row is not None:
        row.qty_in_stock = int(row.qty_in_stock or 0) + qty
        if sp_price is not None:
            row.unit_price = float(sp_price)
    else:
        db.add(SparePart(
            part_code=part_id,
            name=sp_name,
            category=_f(category) if _f(category) else "Other",
            unit_price=float(sp_price) if sp_price is not None else 0.0,
            qty_in_stock=qty,
            min_stock_alert=_i(min_stock_alert) or 5,
            supplier="Internal",
            notes=_f(product_description),
            source="harvest",
            make=_f(part_brand),
            model=_f(part_model),
            crate_number=sp_crate,
        ))
    await db.commit()
    await audit(db, action="HARVEST_PART_ADD", user=current_user,
                table_name="parts_grn_line_items", record_id=part_id)
    return JSONResponse({"ok": True, "part_id": part_id, "grn_number": grn_number,
                         "matched": row is not None})
