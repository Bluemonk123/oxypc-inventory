"""Rentals (2026-10-22).

New Rental: search + tick Ready to Sale tags (or upload a tags CSV), pick the
Billing Entity (Company Setting), Billing To (Business / Individual customer),
duration (number + unit + start date), price (prefilled with the tags' total
Min Selling Price, editable) and notes. Submitting moves the tags
Ready to Sale -> On Rent and adds a row to Rental Records.

Rental Records: list with Summary (printable / Save as PDF) and Tags CSV
downloads; Mark Returned / Cancel move the tags back to Ready to Sale.

Server-side rules (financial-snapshot pattern): quantity and suggested price
are recomputed from the tags on submit (client totals ignored); the rental
number is generated here; Billing Entity details are copied onto the record.
Module key "rentals" — off by default in the permission matrix.
"""
import calendar
import csv
import io
import uuid as _uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_module_perm, verify_csrf
from database import get_db
from models.company import Company
from models.device import STAGE_LABELS, Device, DeviceStage, StageMovement
from models.lot import Lot
from models.rental import BILL_TYPES, DURATION_UNITS, RENTAL_STATUSES, RentalItem, RentalRecord
from models.user import User
from services.audit_engine import audit
from services.ready_pricing import load_ready_rates, tag_min_price, unit_price
from templates_config import templates
from utils.csv_decode import decode_csv_bytes
from utils.timezone import app_now, app_today

router = APIRouter(prefix="/rentals", tags=["rentals"], dependencies=[Depends(verify_csrf)])
allowed = require_module_perm("rentals")
can_delete = require_module_perm("rentals", "edit")   # Delete = Edit permission (admin always)

_UNIT_LABELS = dict(DURATION_UNITS)
_GRADE_ORDER = ["A", "B", "C", "D", "E", "NG"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _ram(d) -> str:
    return f"{d.ram_gb} GB" if d.ram_gb else ""


def _storage(d) -> str:
    if not d.storage_gb:
        return ""
    return f"{d.storage_gb} GB" + (f" {d.storage_type.strip()}" if (d.storage_type or "").strip() else "")


def _grade(d) -> str:
    g = getattr(d.grade, "value", d.grade) if d.grade else ""
    return g or ""


def _tag_json(d, min_price, rented_in=None) -> dict:
    return {
        "barcode": d.barcode, "make": d.brand or "", "model": d.model or "",
        "cpu": (d.cpu or "").strip(), "gen": (d.generation or "").strip(),
        "ram": _ram(d), "storage": _storage(d), "grade": _grade(d),
        "min_price": float(min_price) if min_price is not None else None,
        "held": bool(d.is_blocked) or d.assigned_to_user_id is not None,
    }


async def _ready_tags(db: AsyncSession, barcodes=None, q: str = "", limit: int = 0):
    """Ready to Sale, non-trashed tags with their Min Selling Price (saved Set
    price, else the Ready to Sale formula). By barcodes (case-insensitive) or a
    search term (tag / serial / make / model)."""
    rates = await load_ready_rates(db)
    query = (select(Device, Lot.buying_price, Lot.qty)
             .outerjoin(Lot, Device.lot_id == Lot.id)
             .where(Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed.isnot(True)))
    if barcodes is not None:
        keys = list({b.strip().upper() for b in barcodes if b and b.strip()})
        if not keys:
            return []
        rows = []
        for i in range(0, len(keys), 5000):
            rows += (await db.execute(query.where(func.upper(Device.barcode).in_(keys[i:i + 5000])))).all()
    else:
        like = f"%{q.strip()}%"
        rows = (await db.execute(
            query.where(or_(Device.barcode.ilike(like), Device.serial_no.ilike(like),
                            Device.brand.ilike(like), Device.model.ilike(like)))
            .order_by(Device.barcode).limit(limit or 50))).all()
    out = []
    for d, buying, qty in rows:
        if d.min_selling_price is not None:
            mp = Decimal(d.min_selling_price)
        else:
            mp = tag_min_price(unit_price(d, buying, qty), rates)
        out.append((d, mp))
    return out


async def _lookup(db: AsyncSession, barcodes: list) -> dict:
    """Details for the given tags, plus which were not found / not Ready to Sale."""
    wanted = []
    seen = set()
    for b in barcodes:
        k = (b or "").strip()
        if k and k.upper() not in seen:
            seen.add(k.upper())
            wanted.append(k)
    found = await _ready_tags(db, barcodes=wanted)
    found_keys = {d.barcode.upper() for d, _ in found}
    missing = [b for b in wanted if b.upper() not in found_keys]
    not_ready, not_found = [], []
    if missing:
        stage_of = {bc.upper(): st for bc, st in (await db.execute(
            select(Device.barcode, Device.current_stage)
            .where(func.upper(Device.barcode).in_([m.upper() for m in missing]))
        )).all()}
        for m in missing:
            st = stage_of.get(m.upper())
            if st is None:
                not_found.append(m)
            else:
                not_ready.append({"barcode": m, "stage": STAGE_LABELS.get(st, getattr(st, "value", str(st)))})
    return {"tags": [_tag_json(d, mp) for d, mp in found], "not_found": not_found, "not_ready": not_ready}


def _end_date(start: date, value: int, unit: str) -> date:
    """Last day of the rental (inclusive): start + duration - 1 day."""
    if unit == "days":
        return start + timedelta(days=value - 1)
    months = value * (12 if unit == "years" else 1)
    y, m = divmod(start.month - 1 + months, 12)
    y, m = start.year + y, m + 1
    day = min(start.day, calendar.monthrange(y, m)[1])
    return date(y, m, day) - timedelta(days=1)


def _duration_text(r) -> str:
    unit = _UNIT_LABELS.get(r.duration_unit, r.duration_unit)
    if r.duration_value == 1:
        unit = unit.rstrip("s")
    return f"{r.duration_value} {unit}"


def _model_summary(items) -> list:
    """One row per Make + Model + CPU + Gen + RAM + Storage, with grade counts."""
    groups: dict = {}
    for it in items:
        key = ((it["make"] or "").strip(), (it["model"] or "").strip(), it["cpu"], it["gen"], it["ram"], it["storage"])
        g = groups.setdefault(key, {"make": key[0], "model": key[1], "cpu": key[2], "gen": key[3],
                                    "ram": key[4], "storage": key[5], "qty": 0, "grades": {}, "min_total": Decimal("0")})
        g["qty"] += 1
        gr = it["grade"] or "NG"
        g["grades"][gr] = g["grades"].get(gr, 0) + 1
        if it.get("min_price") is not None:
            g["min_total"] += Decimal(str(it["min_price"]))
    out = sorted(groups.values(), key=lambda g: (-g["qty"], g["model"]))
    for g in out:
        g["grades"] = [(k, g["grades"][k]) for k in _GRADE_ORDER if g["grades"].get(k)]
    return out


def _entity(r, company=None) -> dict:
    """Billing Entity details to SHOW for a rental — LIVE from Company Setting
    (2026-10-22, on request), so an edit there shows on every rental, past ones
    included. The copy saved on the rental is used only if the company row is
    gone."""
    if company is not None:
        return {"name": company.company_name or "", "address": company.company_address or "",
                "gstin": company.company_gstin or "", "state": company.company_state or "",
                "phone": company.company_phone or "", "email": company.company_email or ""}
    return {"name": r.billing_entity_name or "", "address": r.billing_entity_address or "",
            "gstin": r.billing_entity_gstin or "", "state": r.billing_entity_state or "",
            "phone": r.billing_entity_phone or "", "email": r.billing_entity_email or ""}


async def _companies_by_id(db, rentals) -> dict:
    ids = list({r.billing_company_id for r in rentals if r.billing_company_id})
    if not ids:
        return {}
    return {c.id: c for c in (await db.execute(select(Company).where(Company.id.in_(ids)))).scalars().all()}


def _model_line(g) -> str:
    """'Dell Latitude 5420 i5 11th 16 GB 256 GB x2' — one Model Summary row as text."""
    return " ".join(f"{g['make']} {g['model']} {g['cpu']} {g['gen']} {g['ram']} {g['storage']}".split()) + f" x{g['qty']}"


def _item_dict(it: RentalItem) -> dict:
    return {"barcode": it.barcode, "make": it.make or "", "model": it.model or "", "cpu": it.cpu or "",
            "gen": it.generation or "", "ram": it.ram or "", "storage": it.storage or "",
            "grade": it.grade or "", "min_price": float(it.min_selling_price) if it.min_selling_price is not None else None}


async def _next_rental_number(db: AsyncSession) -> str:
    year = app_today().year
    prefix = f"RNT-{year}-"
    last = (await db.execute(
        select(RentalRecord.rental_number).where(RentalRecord.rental_number.like(prefix + "%"))
        .order_by(RentalRecord.rental_number.desc()).limit(1))).scalar_one_or_none()
    n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{n:04d}"


def _dec(v, default=None):
    try:
        return Decimal(str(v).replace(",", "").strip()) if str(v or "").strip() else default
    except (InvalidOperation, ValueError):
        return default


async def _get_rental(db, rental_id):
    try:
        rid = _uuid.UUID(str(rental_id))
    except ValueError:
        raise HTTPException(404, "Rental not found")
    r = (await db.execute(select(RentalRecord).where(RentalRecord.id == rid))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Rental not found")
    return r


async def _move_stage(db, devices, to_stage, username, note, now):
    """Close each device's open StageMovement (exited_at) and log the new one —
    same bookkeeping as the other stage moves, batched in one query."""
    if not devices:
        return
    by_id = {d.id: d for d in devices}
    open_mvs = (await db.execute(
        select(StageMovement).where(StageMovement.device_id.in_(list(by_id)),
                                    StageMovement.exited_at.is_(None))
    )).scalars().all()
    for mv in open_mvs:
        d = by_id.get(mv.device_id)
        if d is not None and mv.to_stage == d.current_stage:
            mv.exited_at = now
    for d in devices:
        db.add(StageMovement(device_id=d.id, from_stage=d.current_stage, to_stage=to_stage,
                             moved_by=username, notes=note))
        d.current_stage = to_stage
        d.updated_at = now


async def _items_of(db, rental_ids) -> dict:
    out: dict = {}
    if not rental_ids:
        return out
    for it in (await db.execute(
        select(RentalItem).where(RentalItem.rental_id.in_(list(rental_ids))).order_by(RentalItem.barcode)
    )).scalars().all():
        out.setdefault(it.rental_id, []).append(it)
    return out


# ── pages ────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def rental_records(request: Request, status: str = Query(default=""),
                         db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    q = select(RentalRecord).order_by(RentalRecord.created_at.desc())
    if status:
        q = q.where(RentalRecord.status == status)
    rentals = (await db.execute(q)).scalars().all()
    items = await _items_of(db, [r.id for r in rentals])
    companies = await _companies_by_id(db, rentals)
    rows = []
    for r in rentals:
        its = items.get(r.id, [])
        ent = _entity(r, companies.get(r.billing_company_id))
        summary = _model_summary([_item_dict(i) for i in its])
        rows.append({"r": r, "ent": ent, "duration": _duration_text(r), "models": summary,
                     "tags": [i.barcode for i in its],
                     "export": {
                         "Rental ID": r.rental_number,
                         "Created On": r.created_at.strftime("%d-%m-%Y") if r.created_at else "",
                         "Status": r.status.capitalize(),
                         "Bill Type": dict(BILL_TYPES).get(r.bill_type, r.bill_type),
                         "Billing Entity": ent["name"],
                         "Billing Entity GSTIN": ent["gstin"],
                         "Billing Entity Address": ent["address"],
                         "Billing Entity State": ent["state"],
                         "Billing Entity Phone": ent["phone"],
                         "Billing Entity Email": ent["email"],
                         "Company Name": r.customer_company or "",
                         "GSTIN": r.customer_gstin or "",
                         "Person Name": r.person_name or "",
                         "Phone": r.phone or "",
                         "Email": r.email or "",
                         "City": r.city or "",
                         "State": r.state or "",
                         "Address": r.address or "",
                         "Models": len(summary),
                         "Model List": "; ".join(_model_line(g) for g in summary),
                         "Tags": len(its),
                         "Tag Numbers": " ".join(i.barcode for i in its),
                         "Duration": _duration_text(r),
                         "Start Date": r.start_date.strftime("%d-%m-%Y"),
                         "End Date": r.end_date.strftime("%d-%m-%Y"),
                         "Quantity": r.total_quantity,
                         "Min Selling Total": str(r.suggested_price) if r.suggested_price is not None else "",
                         "Price": str(r.total_price),
                         "Notes": r.notes or "",
                         "Created By": r.created_by or "",
                         "Closed On": r.closed_at.strftime("%d-%m-%Y") if r.closed_at else "",
                         "Closed By": r.closed_by or "",
                     }})
    return templates.TemplateResponse("rentals/list.html", {
        "request": request, "current_user": current_user, "rows": rows, "status": status,
        "statuses": RENTAL_STATUSES, "bill_types": dict(BILL_TYPES), "today": app_today(),
    })


@router.get("/new", response_class=HTMLResponse)
async def rental_new(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    companies = (await db.execute(
        select(Company).where(Company.is_active.isnot(False)).order_by(Company.company_name))).scalars().all()
    company_json = {str(c.id): {"name": c.company_name, "entity": c.company_entity or "",
                                "address": c.company_address or "", "gstin": c.company_gstin or "",
                                "state": c.company_state or "", "phone": c.company_phone or "",
                                "email": c.company_email or ""} for c in companies}
    return templates.TemplateResponse("rentals/form.html", {
        "request": request, "current_user": current_user, "companies": companies,
        "company_json": company_json, "units": DURATION_UNITS, "today": app_today().isoformat(),
    })


@router.get("/tags/search")
async def rental_tag_search(q: str = Query(default=""), db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(allowed)):
    if len(q.strip()) < 2:
        return JSONResponse({"tags": []})
    found = await _ready_tags(db, q=q, limit=50)
    return JSONResponse({"tags": [_tag_json(d, mp) for d, mp in found]})


@router.post("/tags/details")
async def rental_tag_details(barcodes: str = Form(""), db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(allowed)):
    return JSONResponse(await _lookup(db, barcodes.replace("\n", ",").split(",")))


@router.post("/tags/upload")
async def rental_tag_upload(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(allowed)):
    """CSV / text of Tag Numbers — first column; a header row is skipped."""
    text = decode_csv_bytes(await file.read())
    barcodes = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        v = (row[0] or "").strip().lstrip("﻿")
        if not v or v.lower().replace(" ", "") in ("tag", "tagnumber", "tagno", "barcode", "tag_number"):
            continue
        barcodes.append(v)
    if not barcodes:
        return JSONResponse({"error": "No Tag Numbers found in the file (first column)."})
    return JSONResponse(await _lookup(db, barcodes))


@router.post("/new")
async def rental_create(
    request: Request,
    barcodes: str = Form(""),
    billing_company_id: str = Form(""),
    bill_type: str = Form(""),
    customer_company: str = Form(""), customer_gstin: str = Form(""),
    person_name: str = Form(""), phone: str = Form(""), email: str = Form(""),
    city: str = Form(""), state: str = Form(""), address: str = Form(""),
    duration_value: str = Form(""), duration_unit: str = Form("months"),
    start_date: str = Form(""),
    total_price: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
):
    def back(msg):
        return RedirectResponse(url="/rentals/new?error=" + quote(msg), status_code=302)

    # Billing entity (copied onto the record)
    try:
        company = (await db.execute(select(Company).where(Company.id == _uuid.UUID(billing_company_id)))).scalar_one_or_none()
    except ValueError:
        company = None
    if not company:
        return back("Select the Billing Entity.")
    if bill_type not in dict(BILL_TYPES):
        return back("Choose Billing To: Business or Individual.")
    if not person_name.strip():
        return back("Enter the customer's Person Name.")
    if bill_type == "business" and not customer_company.strip():
        return back("Enter the Company Name for a Business customer.")
    try:
        dur = int(duration_value)
    except ValueError:
        dur = 0
    if dur <= 0 or duration_unit not in _UNIT_LABELS:
        return back("Enter the Rental Duration (a number and a unit).")
    try:
        start = date.fromisoformat(start_date) if start_date else app_today()
    except ValueError:
        return back("Start Date is not a valid date.")

    # Tags: re-checked here — every one must still be Ready to Sale.
    wanted = [b for b in barcodes.replace("\n", ",").split(",") if b.strip()]
    if not wanted:
        return back("Add at least one tag.")
    found = await _ready_tags(db, barcodes=wanted)
    found_keys = {d.barcode.upper() for d, _ in found}
    missing = sorted({b.strip() for b in wanted if b.strip().upper() not in found_keys})
    if missing:
        return back(f"No longer Ready to Sale (or not found): {', '.join(missing[:20])}"
                    + (f" and {len(missing) - 20} more" if len(missing) > 20 else ""))

    quantity = len(found)
    suggested = sum((mp for _d, mp in found if mp is not None), Decimal("0")).quantize(Decimal("0.01"))
    price = _dec(total_price, suggested)
    if price is None or price < 0:
        return back("Total Price must be a number (0 or more).")

    entity = dict(billing_company_id=company.id, billing_entity_name=company.company_name,
                  billing_entity_address=company.company_address, billing_entity_gstin=company.company_gstin,
                  billing_entity_state=company.company_state, billing_entity_phone=company.company_phone,
                  billing_entity_email=company.company_email)
    devices = [d for d, _ in found]
    prices = [mp for _, mp in found]
    device_ids = [d.id for d in devices]
    r = None
    for _attempt in range(3):
        r = RentalRecord(
            rental_number=await _next_rental_number(db), status="active", **entity,
            bill_type=bill_type,
            customer_company=(customer_company.strip()[:200] or None) if bill_type == "business" else None,
            customer_gstin=(customer_gstin.strip().upper()[:20] or None) if bill_type == "business" else None,
            person_name=person_name.strip()[:150], phone=phone.strip()[:20] or None,
            email=email.strip()[:150] or None, city=city.strip()[:100] or None,
            state=state.strip()[:100] or None, address=address.strip() or None,
            duration_value=dur, duration_unit=duration_unit, start_date=start,
            end_date=_end_date(start, dur, duration_unit),
            total_quantity=quantity, suggested_price=suggested, total_price=price.quantize(Decimal("0.01")),
            notes=notes.strip() or None, created_by=current_user.username,
        )
        db.add(r)
        try:
            await db.flush()
            break
        except IntegrityError:   # Rental ID taken by a parallel submit — retry with the next one
            await db.rollback()
            r = None
            devices = [await db.get(Device, i) for i in device_ids]   # reload after rollback
    if r is None:
        return back("Could not allocate a Rental ID — please submit again.")

    now = app_now()
    for d, mp in zip(devices, prices):
        db.add(RentalItem(
            rental_id=r.id, device_id=d.id, barcode=d.barcode, make=d.brand, model=d.model,
            cpu=d.cpu, generation=d.generation, ram=_ram(d) or None, storage=_storage(d) or None,
            grade=_grade(d) or None, min_selling_price=mp,
        ))
    await _move_stage(db, devices, DeviceStage.on_rent, current_user.username,
                      f"On Rent — {r.rental_number}", now)
    await audit(db, user=current_user, action="RENTAL_CREATED", table_name="rental_records",
                record_id=r.rental_number,
                new_value={"rental_number": r.rental_number, "billing_entity": r.billing_entity_name,
                           "bill_type": bill_type, "customer": r.customer_company or r.person_name,
                           "tags": quantity, "duration": f"{dur} {duration_unit}",
                           "start_date": start.isoformat(), "suggested_price": str(suggested),
                           "total_price": str(r.total_price)},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/rentals?success=" + quote(
        f"Rental {r.rental_number} created — {quantity} tag(s) moved to On Rent"), status_code=302)


@router.get("/{rental_id}", response_class=HTMLResponse)
async def rental_view(request: Request, rental_id: str, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(allowed)):
    r = await _get_rental(db, rental_id)
    its = (await _items_of(db, [r.id])).get(r.id, [])
    company = (await _companies_by_id(db, [r])).get(r.billing_company_id)
    return templates.TemplateResponse("rentals/detail.html", {
        "request": request, "current_user": current_user, "r": r, "items": its,
        "ent": _entity(r, company),
        "summary": _model_summary([_item_dict(i) for i in its]), "duration": _duration_text(r),
        "bill_types": dict(BILL_TYPES), "statuses": dict(RENTAL_STATUSES),
    })


@router.get("/{rental_id}/print", response_class=HTMLResponse)
async def rental_print(request: Request, rental_id: str, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(allowed)):
    """Printable rental summary — the browser's Print / Save as PDF."""
    r = await _get_rental(db, rental_id)
    its = (await _items_of(db, [r.id])).get(r.id, [])
    company = (await _companies_by_id(db, [r])).get(r.billing_company_id)
    return templates.TemplateResponse("rentals/print.html", {
        "request": request, "current_user": current_user, "r": r, "items": its,
        "ent": _entity(r, company),
        "summary": _model_summary([_item_dict(i) for i in its]), "duration": _duration_text(r),
        "bill_types": dict(BILL_TYPES), "statuses": dict(RENTAL_STATUSES),
    })


@router.get("/{rental_id}/tags.csv")
async def rental_tags_csv(rental_id: str, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(allowed)):
    """The rental's tags in the All Inventory export format."""
    from routers.devices import _export_rows
    r = await _get_rental(db, rental_id)
    ids = [it.device_id for it in (await _items_of(db, [r.id])).get(r.id, [])]
    rows = []
    for i in range(0, len(ids), 20000):
        rows += (await db.execute(
            select(Device, Lot.lot_number).outerjoin(Lot, Device.lot_id == Lot.id)
            .where(Device.id.in_(ids[i:i + 20000])).order_by(Device.barcode))).all()
    resp = await _export_rows(db, rows)
    resp.headers["Content-Disposition"] = f"attachment; filename={r.rental_number}_tags.csv"
    return resp


async def _close(request, rental_id, new_status, db, current_user):
    r = await _get_rental(db, rental_id)
    if r.status != "active":
        return RedirectResponse(url="/rentals?error=" + quote(f"{r.rental_number} is already {r.status}."),
                                status_code=302)
    ids = [it.device_id for it in (await _items_of(db, [r.id])).get(r.id, [])]
    devices = (await db.execute(select(Device).where(Device.id.in_(ids)))).scalars().all() if ids else []
    now = app_now()
    label = "Returned" if new_status == "returned" else "Cancelled"
    on_rent = [d for d in devices if d.current_stage == DeviceStage.on_rent]   # others moved on since — left untouched
    await _move_stage(db, on_rent, DeviceStage.ready_to_sale, current_user.username,
                      f"Rental {label} — {r.rental_number}", now)
    back = len(on_rent)
    r.status = new_status
    r.closed_at = now
    r.closed_by = current_user.username
    await audit(db, user=current_user, action=f"RENTAL_{new_status.upper()}", table_name="rental_records",
                record_id=r.rental_number,
                old_value={"status": "active"},
                new_value={"status": new_status, "tags_back_to_ready_to_sale": back}, request=request)
    await db.commit()
    return RedirectResponse(url="/rentals?success=" + quote(
        f"Rental {r.rental_number} {label.lower()} — {back} tag(s) back to Ready to Sale"), status_code=302)


@router.post("/{rental_id}/return")
async def rental_return(request: Request, rental_id: str, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed)):
    return await _close(request, rental_id, "returned", db, current_user)


@router.post("/{rental_id}/cancel")
async def rental_cancel(request: Request, rental_id: str, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed)):
    return await _close(request, rental_id, "cancelled", db, current_user)


@router.post("/{rental_id}/delete")
async def rental_delete(request: Request, rental_id: str, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(can_delete)):
    """Permanently delete a rental and its tag lines. Tags still On Rent from
    it go back to Ready to Sale first. The full record is written to the audit
    log before the delete (it is gone after the commit)."""
    r = await _get_rental(db, rental_id)
    its = (await _items_of(db, [r.id])).get(r.id, [])
    ids = [it.device_id for it in its]
    devices = (await db.execute(select(Device).where(Device.id.in_(ids)))).scalars().all() if ids else []
    on_rent = [d for d in devices if d.current_stage == DeviceStage.on_rent] if r.status == "active" else []
    now = app_now()
    await _move_stage(db, on_rent, DeviceStage.ready_to_sale, current_user.username,
                      f"Rental Deleted — {r.rental_number}", now)
    snapshot = {c.name: (str(getattr(r, c.name)) if getattr(r, c.name) is not None else None)
                for c in RentalRecord.__table__.columns}
    snapshot["tags"] = [it.barcode for it in its]
    await audit(db, user=current_user, action="RENTAL_DELETED", table_name="rental_records",
                record_id=r.rental_number, old_value=snapshot,
                new_value={"deleted": True, "tags_back_to_ready_to_sale": len(on_rent)}, request=request)
    number = r.rental_number
    for it in its:
        await db.delete(it)
    await db.flush()
    await db.delete(r)
    await db.commit()
    msg = f"Rental {number} deleted"
    if on_rent:
        msg += f" — {len(on_rent)} tag(s) back to Ready to Sale"
    return RedirectResponse(url="/rentals?success=" + quote(msg), status_code=302)
