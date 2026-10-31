"""
Spare-part sales chain.

Pages:
  /ready-to-sale-parts   Ready to Sale Parts   — sellable "Added As New" parts
  /parts-sale-request    Parts Sale Request    — approve/reject sale requests
  /part-sales/new        New Parts Sale        — mirrors the device New Sale page
  /part-sales            Spare Part Sales      — sold spare parts
  /parts-dashboard       Parts Dashboard       — KPI cards + weekly bar charts

Sell is gated on an APPROVED, not-yet-consumed PartSaleRequest, so one
approval authorises exactly one sale.
"""
import csv
import io
from collections import defaultdict
from datetime import timedelta, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy import select, func, text
from sqlalchemy.exc import ProgrammingError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from html import escape as esc

from templates_config import templates
from database import get_db
from utils.timezone import app_now, app_today
from models.user import User, UserRole
from models.spare_parts import SparePart
from models.part_request import PartRequest, PartSourcingRequest
from models.part_sales import PartSaleRequest, PartSale
from services.audit_engine import audit
from auth.dependencies import require_roles, verify_csrf

router = APIRouter(tags=["part_sales"], dependencies=[Depends(verify_csrf)])

allowed = require_roles(UserRole.admin, UserRole.sales, UserRole.sales_manager,
                        UserRole.spare_parts_manager)


def _as_uuid(v):
    import uuid as _u
    try:
        return _u.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "Invalid id")


async def _consumed_map(db: AsyncSession) -> dict:
    """qty handed over per part — the same 'consumed' figure Part Master shows:
    every handover regardless of status (a row still shows its handover qty
    after moving on to "received"), pooled by name+make+model rather than
    part_id (a name spans several Part Master rows added across separate
    harvest/bulk uploads, and a request is only ever tied to whichever one
    was picked at handover time) and rather than by name ALONE (that pooled
    a Make/Model's handovers into an unrelated Make/Model's part just because
    both happen to share a name like "RAM") — see routers/spare_parts.py's
    parts_list for the full reasoning, including why the join to SparePart
    is an outer join. Keyed by part_id for the caller's convenience, same as
    spare_parts.py's own consumed_by_part."""
    # Row stock mode (Parts Data Fix applied): handovers already came off the
    # crate row, so nothing is netted here any more.
    from services.parts_stock import row_mode
    if await row_mode(db):
        return {}
    rows = (await db.execute(
        select(PartRequest.part_name, SparePart.make, SparePart.model,
               func.coalesce(func.sum(PartRequest.qty_handed_over), 0))
        .outerjoin(SparePart, PartRequest.part_id == SparePart.id)
        .group_by(PartRequest.part_name, SparePart.make, SparePart.model)
    )).all()
    by_group: dict = {}
    for name, make, model, q in rows:
        key = ((name or "").strip().lower(), (make or "").strip().lower(), (model or "").strip().lower())
        by_group[key] = by_group.get(key, 0) + int(q or 0)
    parts = (await db.execute(select(SparePart.id, SparePart.name, SparePart.make, SparePart.model))).all()
    return {
        str(pid): by_group.get(
            ((name or "").strip().lower(), (make or "").strip().lower(), (model or "").strip().lower()), 0)
        for pid, name, make, model in parts
    }


def _stock_of(part, consumed_map) -> int:
    return int(part.qty_in_stock or 0) - consumed_map.get(str(part.id), 0) - int(part.sold_qty or 0)


# ── Ready to Sale Parts ───────────────────────────────────────────────────────
async def _ready_parts_rows(db: AsyncSession):
    """Shared by the page (filter dropdowns) and the /data feed (everything else).
    Small dataset (parts "Added As New"), but built once so both endpoints
    agree on stock/status logic — no drift between the two."""
    parts = (await db.execute(
        select(SparePart)
        .where(SparePart.source == "new", SparePart.is_trashed.is_(False))
        .order_by(SparePart.name)
    )).scalars().all()
    consumed = await _consumed_map(db)

    # Latest request per part decides the Action column state.
    reqs = (await db.execute(
        select(PartSaleRequest).order_by(PartSaleRequest.created_at.desc())
    )).scalars().all()
    latest, approved_open = {}, {}
    for r in reqs:
        latest.setdefault(str(r.part_id), r)
        if r.status == "approved" and not r.is_consumed:
            approved_open.setdefault(str(r.part_id), r)

    rows = []
    for p in parts:
        stock = _stock_of(p, consumed)
        lr = latest.get(str(p.id))
        rows.append({
            "part": p, "stock": stock,
            "status": lr.status if lr else "",
            "can_sell": bool(approved_open.get(str(p.id))) and stock > 0,
            "pending": bool(lr and lr.status == "pending"),
            # Quantity from the latest request, so the page shows what was
            # actually asked for rather than only that a request exists.
            "requested_qty": (lr.qty_requested if lr else None),
        })
    # Ready to Sale Parts only lists parts actually available to sell — a row
    # whose real stock (In Stock − Sold − Consumed) is 0 or negative has
    # nothing left to raise a request or sell against, so it drops off the
    # table entirely rather than sitting there disabled.
    return [r for r in rows if r["stock"] > 0]


@router.get("/ready-to-sale-parts", response_class=HTMLResponse)
async def ready_to_sale_parts(request: Request, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    rows = await _ready_parts_rows(db)
    makes = sorted({row["part"].make for row in rows if row["part"].make})
    models = sorted({row["part"].model for row in rows if row["part"].model})
    return templates.TemplateResponse("parts/ready_to_sale.html", {
        "request": request, "current_user": current_user,
        "makes": makes, "models": models,
    })


@router.get("/ready-to-sale-parts/data")
async def ready_to_sale_parts_data(request: Request,
                                   draw: int = Query(1), start: int = Query(0), length: int = Query(25),
                                   code: str = Query(""), name: str = Query(""),
                                   make: str = Query(""), model: str = Query(""),
                                   db: AsyncSession = Depends(get_db),
                                   current_user: User = Depends(allowed)):
    """DataTables server-side feed for Ready to Sale Parts."""
    rows = await _ready_parts_rows(db)
    # The table's own search box (DataTables search[value]) — was ignored, so
    # typing in it never filtered anything (2026-10-19). Matches Code, Name,
    # Make or Model, on top of the filter-bar fields above.
    q = (request.query_params.get("search[value]") or "").strip().lower()

    def matches(row):
        p = row["part"]
        if q and not any(q in (v or "").lower() for v in (p.part_code, p.name, p.make, p.model)):
            return False
        if code and code.lower() not in (p.part_code or "").lower():
            return False
        if name and name.lower() not in (p.name or "").lower():
            return False
        if make and (p.make or "") != make:
            return False
        if model and (p.model or "") != model:
            return False
        return True

    filtered = [r for r in rows if matches(r)]
    total = len(rows)
    page = filtered[start:start + length]

    def stock_badge(row):
        p = row["part"]
        if row["stock"] <= 0:
            return f'<span class="badge bg-danger">0</span>'
        if row["stock"] <= (p.min_stock_alert or 0):
            return f'<span class="badge bg-warning text-dark">{row["stock"]}</span>'
        return f'<span class="badge bg-success">{row["stock"]}</span>'

    def action_cell(row):
        p = row["part"]
        req_disabled = row["stock"] <= 0 or row["pending"]
        req_title = ("Out of stock" if row["stock"] <= 0
                     else "A request is already pending" if row["pending"]
                     else "Raise a sale request")
        sell_disabled = not row["can_sell"]
        sell_title = ("Out of stock" if row["stock"] <= 0
                      else "Needs an approved sale request" if not row["can_sell"]
                      else "Sell this part")
        status_badge = ""
        if row["status"] == "pending":
            status_badge = '<span class="badge bg-warning text-dark ms-1">Pending</span>'
        elif row["status"] == "approved" and row["can_sell"]:
            status_badge = '<span class="badge bg-success ms-1">Approved</span>'
        elif row["status"] == "rejected":
            status_badge = '<span class="badge bg-danger ms-1">Rejected</span>'
        sell_disabled_attrs = 'tabindex="-1" aria-disabled="true"' if sell_disabled else ""
        return (
            f'<div class="text-nowrap">'
            f'<button type="button" class="btn btn-sm btn-outline-primary py-0 px-2 req-btn" '
            f'data-part-id="{p.id}" data-part-name="{esc(p.name or "")}" '
            f'data-part-code="{esc(p.part_code or "")}" data-stock="{row["stock"]}" '
            f'{"disabled" if req_disabled else ""} title="{esc(req_title)}">'
            f'<i class="bi bi-send me-1"></i>Request</button> '
            f'<a href="/part-sales/new?part_id={p.id}" '
            f'class="btn btn-sm btn-success py-0 px-2{" disabled" if sell_disabled else ""}" '
            f'{sell_disabled_attrs} title="{esc(sell_title)}">'
            f'<i class="bi bi-cart-check me-1"></i>Sell</a>{status_badge}</div>'
        )

    data = []
    for row in page:
        p = row["part"]
        data.append([
            f'<span class="font-monospace fw-bold">{esc(p.part_code or "")}</span>',
            esc(p.name or ""),
            esc(p.make or "—"),
            esc(p.model or "—"),
            stock_badge(row),
            (f'<span class="badge bg-primary">{row["requested_qty"]}</span>'
             if row["requested_qty"] else '<span class="text-muted">—</span>'),
            action_cell(row),
        ])

    return JSONResponse({
        "draw": draw, "recordsTotal": total, "recordsFiltered": len(filtered), "data": data,
    })


@router.post("/ready-to-sale-parts/{part_id}/request")
async def raise_sale_request(part_id: str, request: Request,
                             qty: str = Form("1"),
                             db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(allowed)):
    part = (await db.execute(
        select(SparePart).where(SparePart.id == _as_uuid(part_id))
    )).scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")
    dup = (await db.execute(
        select(PartSaleRequest.id).where(PartSaleRequest.part_id == part.id,
                                         PartSaleRequest.status == "pending")
    )).scalar_one_or_none()
    if dup:
        return RedirectResponse(url="/ready-to-sale-parts?error=A+request+is+already+pending+for+this+part",
                                status_code=302)
    try:
        q = max(1, int(qty))
    except (ValueError, TypeError):
        q = 1
    req = PartSaleRequest(part_id=part.id, part_code=part.part_code, part_name=part.name,
                          make=part.make, model=part.model, qty_requested=q,
                          status="pending", requested_by=current_user.username)
    db.add(req)
    await audit(db, user=current_user, action="PART_SALE_REQUESTED",
                table_name="part_sale_requests", record_id=str(part.id),
                new_value={"part": part.part_code, "qty": q}, request=request)
    await db.commit()
    return RedirectResponse(url="/ready-to-sale-parts?success=Sale+request+raised", status_code=302)


# ── Parts Sale Request ────────────────────────────────────────────────────────
@router.get("/parts-sale-request", response_class=HTMLResponse)
async def parts_sale_requests(request: Request, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    reqs = (await db.execute(
        select(PartSaleRequest).order_by(PartSaleRequest.created_at.desc())
    )).scalars().all()
    consumed = await _consumed_map(db)
    parts = {str(p.id): p for p in (await db.execute(select(SparePart))).scalars().all()}
    rows = [{"r": r, "stock": _stock_of(parts[str(r.part_id)], consumed)
             if str(r.part_id) in parts else 0} for r in reqs]
    return templates.TemplateResponse("parts/sale_requests.html", {
        "request": request, "current_user": current_user, "rows": rows,
    })


@router.post("/parts-sale-request/{req_id}/approve")
async def approve_sale_request(req_id: str, request: Request,
                               db: AsyncSession = Depends(get_db),
                               current_user: User = Depends(allowed)):
    r = (await db.execute(
        select(PartSaleRequest).where(PartSaleRequest.id == _as_uuid(req_id))
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Request not found")
    r.status = "approved"
    r.actioned_at = app_now()
    r.actioned_by = current_user.username
    await audit(db, user=current_user, action="PART_SALE_REQUEST_APPROVED",
                table_name="part_sale_requests", record_id=str(r.id),
                new_value={"part": r.part_code}, request=request)
    await db.commit()
    return RedirectResponse(url="/parts-sale-request?success=Request+approved", status_code=302)


@router.post("/parts-sale-request/{req_id}/reject")
async def reject_sale_request(req_id: str, request: Request, reason: str = Form(""),
                              db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    r = (await db.execute(
        select(PartSaleRequest).where(PartSaleRequest.id == _as_uuid(req_id))
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Request not found")
    r.status = "rejected"
    r.actioned_at = app_now()
    r.actioned_by = current_user.username
    r.reject_reason = (reason or "").strip()[:300] or None
    await audit(db, user=current_user, action="PART_SALE_REQUEST_REJECTED",
                table_name="part_sale_requests", record_id=str(r.id),
                new_value={"part": r.part_code}, request=request)
    await db.commit()
    return RedirectResponse(url="/parts-sale-request?success=Request+rejected", status_code=302)


# ── New Parts Sale ────────────────────────────────────────────────────────────
async def _next_part_sale_number(db: AsyncSession) -> str:
    """Next PS-#### number, creating the sequence if the environment lacks it.

    This is called by the Sell page's GET handler purely to preview the number,
    so a missing sequence used to turn "click Sell" into a 500 — which is exactly
    what happened on the Supabase environment, where migrate_part_sales.py had
    never been run. The table can exist without the sequence because they are
    created by a one-off migration rather than by the schema auto-provisioner,
    so any environment that missed that migration hits it.

    Seeded past the highest existing sale number so recreating it on a database
    that already holds part sales cannot re-issue a number that is in use.
    """
    try:
        seq = (await db.execute(text("SELECT nextval('part_sale_number_seq')"))).scalar()
    except (ProgrammingError, DBAPIError):
        await db.rollback()          # the failed statement poisons the transaction
        highest = (await db.execute(
            text("SELECT COALESCE(MAX(NULLIF(regexp_replace(sale_number, '\\D', '', 'g'), '')::bigint), 0) "
                 "FROM part_sales")
        )).scalar() or 0
        await db.execute(text(
            "CREATE SEQUENCE IF NOT EXISTS part_sale_number_seq START WITH %d" % (int(highest) + 1)))
        await db.commit()
        seq = (await db.execute(text("SELECT nextval('part_sale_number_seq')"))).scalar()
    return f"PS-{seq:04d}"


@router.get("/part-sales/new", response_class=HTMLResponse)
async def part_sale_new_form(request: Request, db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(allowed),
                             part_id: str = Query(default="")):
    parts = (await db.execute(
        select(SparePart)
        .where(SparePart.source == "new", SparePart.is_trashed.is_(False))
        .order_by(SparePart.name)
    )).scalars().all()
    consumed = await _consumed_map(db)
    # Only parts with an approved, unconsumed request may be sold.
    approved = {str(r.part_id) for r in (await db.execute(
        select(PartSaleRequest).where(PartSaleRequest.status == "approved",
                                      PartSaleRequest.is_consumed.is_(False))
    )).scalars().all()}
    options = [{
        "id": str(p.id), "name": p.name, "make": p.make or "", "model": p.model or "",
        "available": _stock_of(p, consumed), "unit_price": float(p.unit_price or 0),
    } for p in parts if str(p.id) in approved and _stock_of(p, consumed) > 0]
    from utils.sales_person import sales_person_options
    return templates.TemplateResponse("parts/sale_new.html", {
        "request": request, "current_user": current_user,
        "sales_person_options": await sales_person_options(db),
        "options": options, "prefill_part_id": part_id,
        "next_sale_number": await _next_part_sale_number(db),
        "today": app_today().isoformat(),
    })


@router.post("/part-sales/new")
async def create_part_sale(request: Request,
                           part_id: str = Form(...),
                           qty: str = Form("1"),
                           sale_price: str = Form(...),
                           customer_name: str = Form(""),
                           sales_person: str = Form(""),
                           customer_phone: str = Form(""),
                           customer_state: str = Form(default=None),
                           customer_address: str = Form(""),
                           invoice_no: str = Form(""),
                           payment_mode: str = Form("cash"),
                           notes: str = Form(""),
                           sale_date: str = Form(""),
                           db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(allowed)):
    part = (await db.execute(
        select(SparePart).where(SparePart.id == _as_uuid(part_id))
    )).scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")

    consumed = await _consumed_map(db)
    available = _stock_of(part, consumed)
    try:
        q = int(qty)
    except (ValueError, TypeError):
        q = 1
    if q < 1:
        q = 1
    # Server-side guard — the form also enforces this, but never trust the client.
    if q > available:
        return RedirectResponse(
            url=f"/part-sales/new?error=Sale+Quantity+({q})+exceeds+Available+Quantity+({available})",
            status_code=302)

    approval = (await db.execute(
        select(PartSaleRequest)
        .where(PartSaleRequest.part_id == part.id, PartSaleRequest.status == "approved",
               PartSaleRequest.is_consumed.is_(False))
        .order_by(PartSaleRequest.created_at)
    )).scalars().first()
    if not approval:
        return RedirectResponse(
            url="/part-sales/new?error=This+part+has+no+approved+sale+request",
            status_code=302)

    try:
        unit = Decimal(str(sale_price or "0"))
    except (InvalidOperation, ValueError):
        return RedirectResponse(url="/part-sales/new?error=Invalid+sale+price", status_code=302)

    stock_unit = Decimal(str(part.unit_price or 0))
    total = unit * q

    # Sale Date defaults to now; a selected date keeps the current time-of-day
    # so ordering within a day still makes sense for a backdated entry.
    now_dt = app_now()
    resolved_sold_at = now_dt
    if sale_date:
        try:
            resolved_sold_at = datetime.combine(
                datetime.strptime(sale_date, "%Y-%m-%d").date(), now_dt.time())
        except ValueError:
            pass

    sale = PartSale(
        sale_number=await _next_part_sale_number(db),
        part_id=part.id, request_id=approval.id,
        part_code=part.part_code, part_name=part.name, make=part.make, model=part.model,
        qty=q, stock_unit_price=stock_unit, sale_unit_price=unit,
        total_sale_price=total, margin=(unit - stock_unit) * q,
        customer_name=customer_name or None, customer_phone=customer_phone or None,
        customer_state=customer_state or None, customer_address=customer_address or None,
        invoice_no=invoice_no or None,
        payment_mode=payment_mode or None, notes=notes or None,
        sold_by=current_user.username, sales_person=sales_person.strip() or None,
        sold_at=resolved_sold_at,
    )
    db.add(sale)
    part.sold_qty = int(part.sold_qty or 0) + q      # feeds Part Master's Sold column
    approval.is_consumed = True                       # one approval, one sale
    await audit(db, user=current_user, action="PART_SALE_CREATED",
                table_name="part_sales", record_id=str(part.id),
                new_value={"sale_number": sale.sale_number, "qty": q,
                           "unit": str(unit), "total": str(total)}, request=request)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/part-sales/new?error={quote('Could not save sale: ' + str(exc)[:150])}",
            status_code=302)
    return RedirectResponse(url=f"/part-sales?success=Sale+{sale.sale_number}+recorded",
                            status_code=302)


# ── Spare Part Sales ──────────────────────────────────────────────────────────
def _parse_simple_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _part_sales_filters(date_from, date_to):
    """Filter clauses shared by the /part-sales page and its CSV export, so
    export can never drift from what the table is showing."""
    w = []
    d_from = _parse_simple_date(date_from)
    if d_from:
        w.append(PartSale.sold_at >= d_from)
    d_to = _parse_simple_date(date_to)
    if d_to:
        w.append(PartSale.sold_at <= d_to.replace(hour=23, minute=59, second=59))
    return w


@router.get("/part-sales", response_class=HTMLResponse)
async def part_sales_list(request: Request,
                          date_from: str = Query(default=""),
                          date_to: str = Query(default=""),
                          db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(allowed)):
    sales = (await db.execute(
        select(PartSale).where(*_part_sales_filters(date_from, date_to))
        .order_by(PartSale.sold_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("parts/sales_list.html", {
        "request": request, "current_user": current_user, "sales": sales,
        "date_from": date_from, "date_to": date_to,
    })


@router.get("/part-sales/export")
async def export_part_sales_filtered(
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """CSV export of exactly what the /part-sales table is currently
    showing — same _part_sales_filters() the page uses, so this can never
    drift from the filtered view."""
    MAX_EXPORT_ROWS = 5000
    sales = (await db.execute(
        select(PartSale).where(*_part_sales_filters(date_from, date_to))
        .order_by(PartSale.sold_at.desc()).limit(MAX_EXPORT_ROWS)
    )).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Sale ID", "Date", "Part Name", "Part Make", "Part Model", "Qty",
                     "Stock Unit Price", "Sale Unit Price", "Margin", "Payment Mode", "Sales Person"])
    for s in sales:
        writer.writerow([
            s.sale_number, s.sold_at.strftime("%d-%b-%Y %H:%M") if s.sold_at else "",
            s.part_name or "", s.make or "", s.model or "", s.qty,
            float(s.stock_unit_price) if s.stock_unit_price is not None else "",
            float(s.sale_unit_price or 0),
            float(s.margin) if s.margin is not None else "",
            s.payment_mode or "", s.sold_by or "",
        ])
    if len(sales) == MAX_EXPORT_ROWS:
        writer.writerow(["# TRUNCATED", f"Export capped at {MAX_EXPORT_ROWS} rows",
                         "", "", "", "", "", "", "", "", ""])
    output.seek(0)
    return Response(
        content=output.getvalue().encode(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=part_sales_filtered.csv"},
    )


# ── Parts Dashboard ───────────────────────────────────────────────────────────
def _week_buckets(n_weeks: int = 8):
    """(labels, [start,end) bounds) for the last n weeks, oldest first."""
    today = app_now().date()
    monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(n_weeks - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        out.append((start.strftime("%d %b"), start, start + timedelta(days=7)))
    return [x[0] for x in out], [(x[1], x[2]) for x in out]


def _bucket_counts(dates, bounds):
    counts = [0] * len(bounds)
    for d in dates:
        if not d:
            continue
        dd = d.date() if hasattr(d, "date") else d
        for i, (s, e) in enumerate(bounds):
            if s <= dd < e:
                counts[i] += 1
                break
    return counts


@router.get("/parts-dashboard", response_class=HTMLResponse)
async def parts_dashboard(request: Request, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(allowed)):
    parts = (await db.execute(
        select(SparePart).where(SparePart.is_trashed.is_(False))
    )).scalars().all()
    consumed = await _consumed_map(db)

    total_qty = sum(max(_stock_of(p, consumed), 0) for p in parts)
    part_types = len(parts)
    below_min = sum(1 for p in parts
                    if 0 < _stock_of(p, consumed) <= int(p.min_stock_alert or 0))
    out_of_stock = sum(1 for p in parts if _stock_of(p, consumed) <= 0)
    total_new = sum(1 for p in parts if (p.source or "new") != "harvest")
    total_harvest = sum(1 for p in parts if p.source == "harvest")
    stock_value = sum(max(_stock_of(p, consumed), 0) * float(p.unit_price or 0) for p in parts)

    labels, bounds = _week_buckets()
    all_reqs = (await db.execute(select(PartRequest))).scalars().all()
    part_req_dates = [r.created_at for r in all_reqs if r.request_type != "faulty"]
    faulty_dates = [r.created_at for r in all_reqs if r.request_type == "faulty"]
    sourcing_dates = [r.created_at for r in
                      (await db.execute(select(PartSourcingRequest))).scalars().all()]
    sold_dates = [s.sold_at for s in (await db.execute(select(PartSale))).scalars().all()]

    return templates.TemplateResponse("parts/dashboard.html", {
        "request": request, "current_user": current_user,
        "cards": {
            "total_qty": total_qty, "part_types": part_types,
            "below_min": below_min, "out_of_stock": out_of_stock,
            "total_new": total_new, "total_harvest": total_harvest,
            "stock_value": stock_value,
        },
        "week_labels": labels,
        "series": {
            "part_requests": _bucket_counts(part_req_dates, bounds),
            "faulty_requests": _bucket_counts(faulty_dates, bounds),
            "sourcing_requests": _bucket_counts(sourcing_dates, bounds),
            "marked_sold": _bucket_counts(sold_dates, bounds),
        },
    })
