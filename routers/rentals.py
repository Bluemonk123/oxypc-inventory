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
from models.rental import (BILL_TYPES, DURATION_UNITS, RENTAL_STATUSES, RETURN_DECISIONS, RentalItem,
                           RentalRecord, RentalReturn, RentalReturnItem)
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

# Rental Records tabs (2026-10-24): tab -> statuses shown. "returned" (old Mark
# Returned) counts as Complete.
RENTAL_TABS = [("active", "Active", ("active",)), ("complete", "Complete", ("complete", "returned")),
               ("cancelled", "Cancelled", ("cancelled",)), ("payment_due", "Payment Due", ("payment_due",))]


@router.get("", response_class=HTMLResponse)
async def rental_records(request: Request, tab: str = Query(default="active"),
                         db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    tabs = {k: st for k, _l, st in RENTAL_TABS}
    if tab not in tabs:
        tab = "active"
    counts = dict((await db.execute(
        select(RentalRecord.status, func.count()).group_by(RentalRecord.status))).all())
    tab_counts = {k: sum(counts.get(x, 0) for x in st) for k, st in tabs.items()}
    q = (select(RentalRecord).where(RentalRecord.status.in_(tabs[tab]))
         .order_by(RentalRecord.created_at.desc()))
    rentals = (await db.execute(q)).scalars().all()
    items = await _items_of(db, [r.id for r in rentals])
    returns = await _returns_of(db, [r.id for r in rentals])
    returned_ids = await _returned_device_ids(db, [r.id for r in rentals])
    companies = await _companies_by_id(db, rentals)
    rows = []
    for r in rentals:
        its = items.get(r.id, [])
        rets = returns.get(r.id, [])
        due = [x for x in rets if x.decision == "payment_due" and x.paid_at is None]
        ent = _entity(r, companies.get(r.billing_company_id))
        summary = _model_summary([_item_dict(i) for i in its])
        rows.append({"r": r, "ent": ent, "duration": _duration_text(r), "models": summary,
                     "tags": [i.barcode for i in its],
                     "returns": rets, "due": due,
                     "due_total": sum((x.outstanding_amount for x in due), Decimal("0")),
                     "out_count": len([i for i in its if i.device_id not in returned_ids.get(r.id, set())]),
                     "refs": list(dict.fromkeys(x.payment_reference for x in rets if x.payment_reference)),
                     "export": {
                         "Rental ID": r.rental_number,
                         "Created On": r.created_at.strftime("%d-%m-%Y") if r.created_at else "",
                         "Status": dict(RENTAL_STATUSES).get(r.status, r.status),
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
                         "Returns": " ".join(x.return_number for x in rets),
                         "Outstanding Due": str(sum((x.outstanding_amount for x in due), Decimal("0"))),
                         "Invoice / UTR": "; ".join(dict.fromkeys(x.payment_reference for x in rets
                                                                  if x.payment_reference)),
                     }})
    return templates.TemplateResponse("rentals/list.html", {
        "request": request, "current_user": current_user, "rows": rows, "tab": tab,
        "tabs": RENTAL_TABS, "tab_counts": tab_counts,
        "statuses": RENTAL_STATUSES, "status_labels": dict(RENTAL_STATUSES),
        "bill_types": dict(BILL_TYPES), "today": app_today(),
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


# ── Rental Return (2026-10-24) ───────────────────────────────────────────────
# Returns > New > "Rental Return" tab. Scan the tags received back (all from one
# rental; some or all of its tags), pick the Received Date, then Payment Due
# (receipt; Mark Paid later with an Invoice / UTR number) or Waive Off. Amounts
# are worked out here, never taken from the page (financial-snapshot pattern).

OPEN_STATUSES = ("active", "payment_due")


async def _returned_device_ids(db, rental_ids) -> dict:
    """{rental_id: {device ids already received back in a Rental Return}}."""
    out: dict = {}
    if not rental_ids:
        return out
    for rid, did in (await db.execute(
        select(RentalReturn.rental_id, RentalReturnItem.device_id)
        .join(RentalReturnItem, RentalReturnItem.rental_return_id == RentalReturn.id)
        .where(RentalReturn.rental_id.in_(list(rental_ids)))
    )).all():
        out.setdefault(rid, set()).add(did)
    return out


async def _returns_of(db, rental_ids) -> dict:
    out: dict = {}
    if not rental_ids:
        return out
    for x in (await db.execute(
        select(RentalReturn).where(RentalReturn.rental_id.in_(list(rental_ids)))
        .order_by(RentalReturn.created_at)
    )).scalars().all():
        out.setdefault(x.rental_id, []).append(x)
    return out


def _return_amounts(r: RentalRecord, n_tags: int, received: date) -> dict:
    """Server-side amounts for returning n_tags of rental r on `received`."""
    days = max(1, (r.end_date - r.start_date).days + 1)
    qty = max(1, r.total_quantity or 1)
    per_tag = Decimal(r.total_price or 0) / qty
    daily = per_tag / days
    extra = max(0, (received - r.end_date).days)
    return {
        "rental_days": days,
        "daily_rate": daily.quantize(Decimal("0.0001")),
        "rental_price": (per_tag * n_tags).quantize(Decimal("0.01")),
        "extra_days": extra,
        "outstanding": (daily * extra * n_tags).quantize(Decimal("0.01")),
    }


async def _refresh_status(db, r: RentalRecord, username: str, now) -> None:
    """Rental status after a return / Mark Paid: Payment Due while any return
    is unpaid; otherwise Active while tags are still out, else Complete."""
    if r.status == "cancelled":
        return
    rets = (await _returns_of(db, [r.id])).get(r.id, [])
    unpaid = any(x.decision == "payment_due" and x.paid_at is None for x in rets)
    item_ids = {it.device_id for it in (await _items_of(db, [r.id])).get(r.id, [])}
    remaining = item_ids - (await _returned_device_ids(db, [r.id])).get(r.id, set())
    new = "payment_due" if unpaid else ("active" if remaining else "complete")
    r.status = new
    if not remaining and r.closed_at is None:
        r.closed_at = now
        r.closed_by = username


async def _next_return_number(db: AsyncSession) -> str:
    prefix = f"RRT-{app_today().year}-"
    last = (await db.execute(
        select(RentalReturn.return_number).where(RentalReturn.return_number.like(prefix + "%"))
        .order_by(RentalReturn.return_number.desc()).limit(1))).scalar_one_or_none()
    n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{n:04d}"


def _rental_brief(r: RentalRecord) -> dict:
    return {
        "id": str(r.id), "number": r.rental_number, "status": r.status,
        "start_date": r.start_date.isoformat(), "stop_date": r.end_date.isoformat(),
        "start_text": r.start_date.strftime("%d-%m-%Y"), "stop_text": r.end_date.strftime("%d-%m-%Y"),
        "rental_days": max(1, (r.end_date - r.start_date).days + 1),
        "total_quantity": r.total_quantity, "total_price": str(r.total_price),
        "bill_type": dict(BILL_TYPES).get(r.bill_type, r.bill_type),
        "customer_company": r.customer_company or "", "customer_gstin": r.customer_gstin or "",
        "person_name": r.person_name or "", "phone": r.phone or "", "email": r.email or "",
        "city": r.city or "", "state": r.state or "", "address": r.address or "",
    }


async def _open_rental_of_tag(db, barcode: str):
    """(device, rental, error) for a tag out on an open rental and not yet returned."""
    bc = (barcode or "").strip()
    if not bc:
        return None, None, "Enter a Tag Number."
    device = (await db.execute(select(Device).where(func.upper(Device.barcode) == bc.upper()))).scalars().first()
    if not device:
        return None, None, f"{bc}: tag not found."
    rows = (await db.execute(
        select(RentalRecord).join(RentalItem, RentalItem.rental_id == RentalRecord.id)
        .where(RentalItem.device_id == device.id, RentalRecord.status.in_(OPEN_STATUSES))
        .order_by(RentalRecord.created_at.desc())
    )).scalars().all()
    returned = await _returned_device_ids(db, [x.id for x in rows])
    for r in rows:
        if device.id not in returned.get(r.id, set()):
            return device, r, None
    return device, None, f"{device.barcode}: not out on an open rental (or already returned)."


@router.get("/return/lookup")
async def rental_return_lookup(barcode: str = Query(default=""), db: AsyncSession = Depends(get_db),
                               current_user: User = Depends(allowed)):
    device, r, err = await _open_rental_of_tag(db, barcode)
    if err:
        return JSONResponse({"ok": False, "error": err})
    return JSONResponse({"ok": True, "tag": {"barcode": device.barcode, "make": device.brand or "",
                                              "model": device.model or ""},
                         "rental": _rental_brief(r)})


@router.get("/return/rental/{rental_id}")
async def rental_return_prefill(rental_id: str, db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(allowed)):
    """The tags of one rental still out — the Rental Return tab opened from
    Rental Records' Return button starts with these."""
    r = await _get_rental(db, rental_id)
    if r.status not in OPEN_STATUSES:
        return JSONResponse({"ok": False, "error": f"{r.rental_number} is {r.status}."})
    done = (await _returned_device_ids(db, [r.id])).get(r.id, set())
    tags = [{"barcode": it.barcode, "make": it.make or "", "model": it.model or ""}
            for it in (await _items_of(db, [r.id])).get(r.id, []) if it.device_id not in done]
    return JSONResponse({"ok": True, "rental": _rental_brief(r), "tags": tags})


@router.post("/return/submit")
async def rental_return_submit(
    request: Request,
    barcodes: str = Form(""),
    received_date: str = Form(""),
    decision: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
):
    def back(msg):
        return RedirectResponse(url="/returns/new?active_tab=rental&error=" + quote(msg), status_code=302)

    if decision not in dict(RETURN_DECISIONS):
        return back("Choose Payment Due or Waive Off.")
    try:
        received = date.fromisoformat(received_date)
    except ValueError:
        return back("Pick the Received Date.")
    wanted, seen = [], set()
    for b in barcodes.replace("\n", ",").split(","):
        b = b.strip()
        if b and b.upper() not in seen:
            seen.add(b.upper())
            wanted.append(b)
    if not wanted:
        return back("Scan at least one Tag Number.")
    rental, devices, errors = None, [], []
    for b in wanted:
        device, r, err = await _open_rental_of_tag(db, b)
        if err:
            errors.append(err)
            continue
        if rental is None:
            rental = r
        elif r.id != rental.id:
            errors.append(f"{device.barcode} is on {r.rental_number}, not {rental.rental_number} — "
                          "return one rental at a time.")
            continue
        devices.append(device)
    if errors:
        return back(" ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))
    if received < rental.start_date:
        return back("Received Date can't be before the Rental Start Date.")

    amt = _return_amounts(rental, len(devices), received)
    now = app_now()
    ret = None
    for _attempt in range(3):
        ret = RentalReturn(
            return_number=await _next_return_number(db), rental_id=rental.id, received_date=received,
            stop_date=rental.end_date, rental_price=amt["rental_price"], daily_rate=amt["daily_rate"],
            extra_days=amt["extra_days"], outstanding_amount=amt["outstanding"], decision=decision,
            notes=notes.strip() or None, created_by=current_user.username)
        db.add(ret)
        try:
            await db.flush()
            break
        except IntegrityError:
            await db.rollback()
            ret = None
            rental = await db.get(RentalRecord, rental.id)
            devices = [await db.get(Device, d_id) for d_id in [d.id for d in devices]]
    if ret is None:
        return back("Could not allocate a Return number — please submit again.")
    for d in devices:
        db.add(RentalReturnItem(rental_return_id=ret.id, device_id=d.id, barcode=d.barcode))
    on_rent = [d for d in devices if d.current_stage == DeviceStage.on_rent]
    await _move_stage(db, on_rent, DeviceStage.ready_to_sale, current_user.username,
                      f"Rental Return {ret.return_number} — {rental.rental_number}", now)
    await db.flush()
    await _refresh_status(db, rental, current_user.username, now)
    await audit(db, user=current_user, action="RENTAL_RETURN", table_name="rental_returns",
                record_id=ret.return_number,
                new_value={"rental": rental.rental_number, "tags": [d.barcode for d in devices],
                           "received_date": received.isoformat(), "stop_date": rental.end_date.isoformat(),
                           "rental_price": str(amt["rental_price"]), "daily_rate": str(amt["daily_rate"]),
                           "extra_days": amt["extra_days"], "outstanding": str(amt["outstanding"]),
                           "decision": decision, "rental_status": rental.status},
                request=request)
    await db.commit()
    tab = "payment_due" if rental.status == "payment_due" else ("complete" if rental.status == "complete" else "active")
    msg = (f"{ret.return_number}: {len(devices)} tag(s) of {rental.rental_number} received back — "
           + (f"₹{amt['outstanding']:,.2f} Payment Due" if decision == "payment_due" else "Waived Off")
           + (f". {rental.rental_number} is still Active (tags still out)." if rental.status == "active" else "."))
    return RedirectResponse(url=f"/rentals?tab={tab}&success=" + quote(msg), status_code=302)


@router.get("/returns/{return_id}/receipt", response_class=HTMLResponse)
async def rental_return_receipt(request: Request, return_id: str, db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(allowed)):
    """Payment Due receipt — printable (Print / Save as PDF)."""
    try:
        rid = _uuid.UUID(return_id)
    except ValueError:
        raise HTTPException(404, "Return not found")
    ret = (await db.execute(select(RentalReturn).where(RentalReturn.id == rid))).scalar_one_or_none()
    if not ret:
        raise HTTPException(404, "Return not found")
    r = await db.get(RentalRecord, ret.rental_id)
    items = (await db.execute(select(RentalReturnItem).where(RentalReturnItem.rental_return_id == ret.id)
                              .order_by(RentalReturnItem.barcode))).scalars().all()
    rent_items = {it.device_id: it for it in (await _items_of(db, [r.id])).get(r.id, [])}
    company = (await _companies_by_id(db, [r])).get(r.billing_company_id)
    return templates.TemplateResponse("rentals/return_receipt.html", {
        "request": request, "current_user": current_user, "ret": ret, "r": r,
        "lines": [{"barcode": i.barcode, "it": rent_items.get(i.device_id)} for i in items],
        "ent": _entity(r, company), "decisions": dict(RETURN_DECISIONS),
        "rental_days": max(1, (r.end_date - r.start_date).days + 1),
    })


@router.post("/{rental_id}/paid")
async def rental_mark_paid(request: Request, rental_id: str, payment_reference: str = Form(""),
                           db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    """Mark Paid: every unpaid Payment Due return of this rental gets the
    Invoice / UTR number; the rental then moves on (Complete, or Active while
    tags are still out)."""
    r = await _get_rental(db, rental_id)
    ref = payment_reference.strip()[:100]
    if not ref:
        return RedirectResponse(url="/rentals?tab=payment_due&error=" + quote("Enter the Invoice or UTR number."),
                                status_code=302)
    unpaid = [x for x in (await _returns_of(db, [r.id])).get(r.id, [])
              if x.decision == "payment_due" and x.paid_at is None]
    if not unpaid:
        return RedirectResponse(url="/rentals?tab=payment_due&error=" + quote(f"{r.rental_number} has nothing due."),
                                status_code=302)
    now = app_now()
    for x in unpaid:
        x.paid_at = now
        x.paid_by = current_user.username
        x.payment_reference = ref
    await db.flush()
    await _refresh_status(db, r, current_user.username, now)
    await audit(db, user=current_user, action="RENTAL_PAYMENT_RECEIVED", table_name="rental_returns",
                record_id=r.rental_number, old_value={"status": "payment_due"},
                new_value={"returns": [x.return_number for x in unpaid], "payment_reference": ref,
                           "amount": str(sum((x.outstanding_amount for x in unpaid), Decimal("0"))),
                           "rental_status": r.status},
                request=request)
    await db.commit()
    tab = "complete" if r.status == "complete" else "active"
    return RedirectResponse(url=f"/rentals?tab={tab}&success=" + quote(
        f"{r.rental_number} marked paid ({ref}) — now {dict(RENTAL_STATUSES).get(r.status, r.status)}."),
        status_code=302)


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


@router.get("/{rental_id}/summary")
async def rental_summary_json(rental_id: str, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    """Rental Records' Quantity modal (2026-10-24): the detail page's Tags and
    Model Summary tables as JSON."""
    r = await _get_rental(db, rental_id)
    its = (await _items_of(db, [r.id])).get(r.id, [])
    return JSONResponse({
        "number": r.rental_number,
        "tags": [{"barcode": it.barcode, "make": it.make or "", "model": it.model or "", "cpu": it.cpu or "",
                  "gen": it.generation or "", "ram": it.ram or "", "storage": it.storage or "",
                  "grade": it.grade or "",
                  "min_price": float(it.min_selling_price) if it.min_selling_price is not None else None}
                 for it in its],
        "models": [{"make": g["make"], "model": g["model"], "cpu": g["cpu"], "gen": g["gen"], "ram": g["ram"],
                    "storage": g["storage"], "qty": g["qty"], "grades": g["grades"]}
                   for g in _model_summary([_item_dict(i) for i in its])],
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
    return RedirectResponse(url=f"/rentals?tab={'cancelled' if new_status == 'cancelled' else 'complete'}&success="
                            + quote(f"Rental {r.rental_number} {label.lower()} — {back} tag(s) back to Ready to Sale"),
                            status_code=302)


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
    on_rent = ([d for d in devices if d.current_stage == DeviceStage.on_rent]
               if r.status in ("active", "payment_due") else [])
    now = app_now()
    await _move_stage(db, on_rent, DeviceStage.ready_to_sale, current_user.username,
                      f"Rental Deleted — {r.rental_number}", now)
    snapshot = {c.name: (str(getattr(r, c.name)) if getattr(r, c.name) is not None else None)
                for c in RentalRecord.__table__.columns}
    snapshot["tags"] = [it.barcode for it in its]
    rets = (await db.execute(select(RentalReturn).where(RentalReturn.rental_id == r.id))).scalars().all()
    if rets:
        snapshot["returns"] = [{"return_number": x.return_number, "received_date": str(x.received_date),
                                "outstanding": str(x.outstanding_amount), "decision": x.decision,
                                "payment_reference": x.payment_reference} for x in rets]
    await audit(db, user=current_user, action="RENTAL_DELETED", table_name="rental_records",
                record_id=r.rental_number, old_value=snapshot,
                new_value={"deleted": True, "tags_back_to_ready_to_sale": len(on_rent)}, request=request)
    number = r.rental_number
    if rets:
        for ri in (await db.execute(select(RentalReturnItem).where(
                RentalReturnItem.rental_return_id.in_([x.id for x in rets])))).scalars().all():
            await db.delete(ri)
        await db.flush()
        for x in rets:
            await db.delete(x)
        await db.flush()
    for it in its:
        await db.delete(it)
    await db.flush()
    await db.delete(r)
    await db.commit()
    msg = f"Rental {number} deleted"
    if on_rent:
        msg += f" — {len(on_rent)} tag(s) back to Ready to Sale"
    return RedirectResponse(url="/rentals?success=" + quote(msg), status_code=302)
