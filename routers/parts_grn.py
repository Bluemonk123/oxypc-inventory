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
    return templates.TemplateResponse("spare_parts/parts_grn_list.html", {
        "request": request, "current_user": current_user, "grns": grns,
        "error": error, "success": success,
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
                ("Invoice Date", invoice_date), ("E-Way Bill Number", eway_bill_number),
                ("E-Way Bill Date", eway_bill_date), ("Vehicle Number", vehicle_number),
                ("Invoice Price", invoice_value), ("PO Quantity", total_po_qty),
                ("Invoice Quantity", total_invoice_qty), ("Physical Quantity", total_physical_qty)]
        _miss = [lbl for lbl, v in _req if not (v or "").strip()]
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
        sp_price = _d(item.get("price"))
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
    from services.parts_stock import find_row, latest_grn_price, crate_conflict, conflict_text
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
    # Model + this Crate when it exists, else create it. Price follows the
    # latest Parts GRN unit price of the same Part Name + Make + Model; the
    # entered price is used only when that part has never come in on a GRN.
    row = await find_row(db, sp_name, _f(part_brand), _f(part_model), "harvest", sp_crate)
    grn_price = await latest_grn_price(db, sp_name, _f(part_brand), _f(part_model))
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
