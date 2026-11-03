from templates_config import templates
from datetime import datetime, date
from utils.timezone import app_now
from utils.price_export import strip_price_columns as _strip_prices   # Pricing Export (2026-11-03)
import csv
import io
from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import selectinload
from database import get_db
from utils.call_outcomes import (
    tally as _tally_outcomes,
    interested_total as _interested_total,
    variants_for as _outcome_variants,
    normalized_column as _norm_outcome_col,
)
from models.telecalling import TelecallingRecord, TelecallingSession
from models.dealers import Dealer, DealerCall
from models.user import User, UserRole
from auth.dependencies import get_current_user, verify_csrf, require_module_perm

router = APIRouter(prefix="/telecalling", tags=["telecalling"], dependencies=[Depends(verify_csrf)])


@router.get("", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = Query(default=""),
    assigned: str = Query(default=""),
    city: str = Query(default=""),
    outcome: str = Query(default=""),
    followup_from: str = Query(default=""),
    followup_to: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = app_now().date()
    from sqlalchemy import case as sa_case

    # ── Follow-ups due today — always today-scoped regardless of filter bar ──
    # Note: .join() is needed for the WHERE condition on Dealer fields.
    # selectinload handles eager-loading of rec.dealer for template access.
    fu_stmt = (
        select(DealerCall)
        .options(selectinload(DealerCall.dealer))
        .join(Dealer, DealerCall.dealer_id == Dealer.id)
        # Calls belonging to a trashed dealer are hidden. Showing them left
        # rows on this page that could not be reached or removed from Dealer
        # Management, so deleting a dealer looked like it had done nothing.
        .where(Dealer.trashed_at.is_(None))
        .where(
            func.date(DealerCall.next_followup_date) == today,
            DealerCall.call_outcome != 'not_interested',
        )
        .order_by(DealerCall.next_followup_date)
    )
    if current_user.role == UserRole.telecaller:
        # Sourcing Sales (UserRole.sales) sees every user's follow-ups, same
        # as admin/sales_manager — only Telecaller stays scoped to their own.
        fu_stmt = fu_stmt.where(DealerCall.called_by == current_user.username)
    followups_due = (await db.execute(fu_stmt)).scalars().all()

    # ── Build shared filters for BOTH KPI cards and Recent Calls table ───────
    if not date_from:
        date_from = today.isoformat()
    if not date_to:
        date_to = today.isoformat()

    recent_filters = [
        func.date(DealerCall.call_date) >= datetime.strptime(date_from, "%Y-%m-%d").date(),
        func.date(DealerCall.call_date) <= datetime.strptime(date_to, "%Y-%m-%d").date(),
    ]

    if assigned:
        recent_filters.append(DealerCall.called_by == assigned)
    elif current_user.role not in (UserRole.admin, UserRole.sales_manager, UserRole.sales):
        recent_filters.append(DealerCall.called_by == current_user.username)

    if outcome:
        # Match every spelling that folds onto this outcome — an exact compare
        # would return nothing for rows the bulk upload stored as "not connected".
        recent_filters.append(
            _norm_outcome_col(DealerCall.call_outcome).in_(_outcome_variants(outcome)))

    if followup_from:
        try:
            recent_filters.append(
                DealerCall.next_followup_date >= datetime.strptime(followup_from, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if followup_to:
        try:
            recent_filters.append(
                DealerCall.next_followup_date <= datetime.strptime(followup_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            )
        except ValueError:
            pass

    dealer_filters = []
    if q:
        like = f"%{q}%"
        dealer_filters.append(
            or_(
                Dealer.business_name.ilike(like),
                Dealer.phone.ilike(like),
                Dealer.contact_person.ilike(like),
            )
        )
    if city:
        dealer_filters.append(Dealer.city.ilike(f"%{city}%"))

    # ── KPI cards — aggregate from the SAME filtered dataset as the table ────
    # Grouped by RAW outcome, then folded onto canonical keys in Python.
    # Matching on exact strings here silently missed every bulk-uploaded call:
    # the sheet says "not connected" / "call back", the in-app form writes
    # "no_answer" / "callback", so these cards read ~0 against 1,051 real calls.
    stat_stmt = (
        select(DealerCall.call_outcome, func.count(DealerCall.id))
        .select_from(DealerCall)
        .join(Dealer, DealerCall.dealer_id == Dealer.id)
        # Calls belonging to a trashed dealer are hidden. Showing them left
        # rows on this page that could not be reached or removed from Dealer
        # Management, so deleting a dealer looked like it had done nothing.
        .where(Dealer.trashed_at.is_(None))
        .where(*recent_filters)
    )
    if dealer_filters:
        stat_stmt = stat_stmt.where(*dealer_filters)

    stat_stmt = stat_stmt.group_by(DealerCall.call_outcome)
    stat_rows = (await db.execute(stat_stmt)).all()
    counts = _tally_outcomes((row[0], row[1]) for row in stat_rows)
    today_stats = {
        # Total counts every row, including calls logged with no outcome yet —
        # tally() drops blanks, so summing its values would under-report.
        "total":          sum(int(r[1] or 0) for r in stat_rows),
        # Interested absorbs Detail Sent — see interested_total()
        "interested":     _interested_total(counts),
        "callback":       counts.get("callback", 0),
        "not_interested": counts.get("not_interested", 0),
        "not_connected":  counts.get("not_connected", 0),
        # Connected = the call was actually answered. Someone who sent details,
        # hit a language barrier, said "no requirement" or asked for a callback
        # all picked up the phone; Not Connected / Busy did not.
        "connected":      (counts.get("details_sent", 0)
                           + counts.get("language_barrier", 0)
                           + counts.get("no_requirement", 0)
                           + counts.get("callback", 0)),
    }

    # ── Recent calls table — same filters, all matching rows (client-side
    # DataTables pagination handles display). Note: .join() enables Dealer.*
    # filtering (q, city). selectinload handles eager-loading for template access.
    recent_stmt = (
        select(DealerCall)
        .options(selectinload(DealerCall.dealer))
        .join(Dealer, DealerCall.dealer_id == Dealer.id)
        # Calls belonging to a trashed dealer are hidden. Showing them left
        # rows on this page that could not be reached or removed from Dealer
        # Management, so deleting a dealer looked like it had done nothing.
        .where(Dealer.trashed_at.is_(None))
        .where(*recent_filters)
    )
    if dealer_filters:
        recent_stmt = recent_stmt.where(*dealer_filters)

    recent_stmt = recent_stmt.order_by(DealerCall.call_date.desc())
    recent_calls = (await db.execute(recent_stmt)).scalars().all()

    # ── Sales users list for admin/manager agent-filter dropdown ─────────────
    sales_users: list = []
    if current_user.role in (UserRole.admin, UserRole.sales_manager, UserRole.sales):
        su_result = await db.execute(
            select(User).where(
                User.role.in_([UserRole.sales, UserRole.sales_manager, UserRole.telecaller]),
                User.status == True,
            ).order_by(User.full_name)
        )
        sales_users = su_result.scalars().all()

    return templates.TemplateResponse("telecalling/index.html", {
        "request": request,
        "current_user": current_user,
        "today_stats": today_stats,
        "followups_due": followups_due,
        "recent_calls": recent_calls,
        "today": today,
        "sales_users": sales_users,
        "q": q,
        "assigned": assigned,
        "city": city,
        "outcome": outcome,
        "followup_from": followup_from,
        "followup_to": followup_to,
        "date_from": date_from,
        "date_to": date_to,
    })


@router.get("/export-csv")
async def export_calls_csv(
    request: Request,
    q: str = Query(default=""),
    assigned: str = Query(default=""),
    city: str = Query(default=""),
    outcome: str = Query(default=""),
    followup_from: str = Query(default=""),
    followup_to: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export filtered Recent Calls as CSV — no row cap, full call + dealer details."""
    today = app_now().date()

    if not date_from:
        date_from = today.isoformat()
    if not date_to:
        date_to = today.isoformat()

    # ── Same filter logic as the dashboard index route ────────────────────────
    recent_filters = [
        func.date(DealerCall.call_date) >= datetime.strptime(date_from, "%Y-%m-%d").date(),
        func.date(DealerCall.call_date) <= datetime.strptime(date_to, "%Y-%m-%d").date(),
    ]

    if assigned:
        recent_filters.append(DealerCall.called_by == assigned)
    elif current_user.role not in (UserRole.admin, UserRole.sales_manager, UserRole.sales):
        recent_filters.append(DealerCall.called_by == current_user.username)

    if outcome:
        # Match every spelling that folds onto this outcome — an exact compare
        # would return nothing for rows the bulk upload stored as "not connected".
        recent_filters.append(
            _norm_outcome_col(DealerCall.call_outcome).in_(_outcome_variants(outcome)))

    if followup_from:
        try:
            recent_filters.append(
                DealerCall.next_followup_date >= datetime.strptime(followup_from, "%Y-%m-%d")
            )
        except ValueError:
            pass
    if followup_to:
        try:
            recent_filters.append(
                DealerCall.next_followup_date <= datetime.strptime(followup_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            )
        except ValueError:
            pass

    dealer_filters = []
    if q:
        like = f"%{q}%"
        dealer_filters.append(
            or_(
                Dealer.business_name.ilike(like),
                Dealer.phone.ilike(like),
                Dealer.contact_person.ilike(like),
            )
        )
    if city:
        dealer_filters.append(Dealer.city.ilike(f"%{city}%"))

    # No LIMIT — export all matching records
    stmt = (
        select(DealerCall)
        .options(selectinload(DealerCall.dealer))
        .join(Dealer, DealerCall.dealer_id == Dealer.id)
        # Calls belonging to a trashed dealer are hidden. Showing them left
        # rows on this page that could not be reached or removed from Dealer
        # Management, so deleting a dealer looked like it had done nothing.
        .where(Dealer.trashed_at.is_(None))
        .where(*recent_filters)
    )
    if dealer_filters:
        stmt = stmt.where(*dealer_filters)

    stmt = stmt.order_by(DealerCall.call_date.desc())
    calls = (await db.execute(stmt)).scalars().all()

    # ── Build CSV ─────────────────────────────────────────────────────────────
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        # Call log fields
        "Call Date", "Call Time",
        "Call Type", "Call Mode", "Duration (mins)",
        "Outcome", "Items Discussed",
        "Quote Given (₹)", "Next Follow-up Date",
        "WhatsApp Sent", "Notes", "Called By",
        # Dealer fields
        "Dealer Code", "Business Name",
        "Contact Person", "Phone", "WhatsApp Number",
        "Email", "City", "State", "Pincode",
        "Dealer Type", "GSTIN",
        "Preferred Categories", "Status",
        "Credit Limit (₹)", "Outstanding (₹)",
        "Total Purchases (₹)",
    ])

    for call in calls:
        d = call.dealer
        writer.writerow([
            # Call log
            call.call_date.strftime("%Y-%m-%d") if call.call_date else "",
            call.call_date.strftime("%H:%M") if call.call_date else "",
            call.call_type or "",
            call.call_mode or "",
            call.duration_mins or "",
            (call.call_outcome or "").replace("_", " ").title(),
            call.items_discussed or "",
            float(call.quote_given) if call.quote_given else "",
            call.next_followup_date.strftime("%Y-%m-%d") if call.next_followup_date else "",
            "Yes" if call.whatsapp_sent else "No",
            call.notes or "",
            call.called_by or "",
            # Dealer
            d.dealer_code if d else "",
            d.business_name if d else "",
            d.contact_person if d else "",
            d.phone if d else "",
            d.whatsapp_number if d else "",
            d.email if d else "",
            d.city if d else "",
            d.state if d else "",
            d.pincode if d else "",
            (d.dealer_type or "").title() if d else "",
            d.gstin if d else "",
            d.preferred_categories if d else "",
            (d.status or "").title() if d else "",
            float(d.credit_limit) if d and d.credit_limit else "",
            float(d.outstanding_amount) if d and d.outstanding_amount else "",
            float(d.total_purchases) if d and d.total_purchases else "",
        ])

    from datetime import datetime as _dt
    fname = f"telecalling-calls-{date_from}-to-{date_to}.csv"
    return await _strip_prices(StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    ), current_user, "telecalling", ["Quote Given (₹)", "Credit Limit (₹)", "Outstanding (₹)", "Total Purchases (₹)"])


@router.get("/add", response_class=HTMLResponse)
async def add_form(
    request: Request,
    dealer_id: str = Query(default=None),
    phone: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dealers_result = await db.execute(
        select(Dealer).where(Dealer.status == "active").order_by(Dealer.business_name)
    )
    dealers = dealers_result.scalars().all()
    return templates.TemplateResponse("telecalling/add.html", {
        "request": request,
        "current_user": current_user,
        "dealers": dealers,
        "prefill_dealer_id": dealer_id or "",
        "prefill_phone": phone,
    })


@router.post("/add")
async def add_record(
    dealer_id: str = Form(default=None),
    dealer_name: str = Form(default=None),
    phone: str = Form(...),
    call_outcome: str = Form(...),
    product_interest: str = Form(default=None),
    quantity_required: str = Form(default=None),
    budget: str = Form(default=None),
    next_followup: str = Form(default=None),
    whatsapp_sent: str = Form(default=None),
    notes: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _perm: User = Depends(require_module_perm("telecalling", "add")),
):
    next_dt = None
    if next_followup:
        try:
            next_dt = datetime.fromisoformat(next_followup)
        except ValueError:
            pass

    _qty = int(quantity_required) if quantity_required and quantity_required.strip() else None
    _budget = float(budget) if budget and budget.strip() else None

    rec = TelecallingRecord(
        dealer_id=dealer_id if dealer_id else None,
        dealer_name=dealer_name,
        phone=phone,
        called_by=current_user.username,
        call_outcome=call_outcome,
        product_interest=product_interest,
        quantity_required=_qty,
        budget=_budget,
        next_followup=next_dt,
        whatsapp_sent=bool(whatsapp_sent),
        notes=notes,
    )
    db.add(rec)
    await db.commit()
    return RedirectResponse(url="/telecalling?success=Call+logged", status_code=302)


@router.get("/records", response_class=HTMLResponse)
async def records(
    request: Request,
    date_from: str = Query(default=None),
    date_to: str = Query(default=None),
    agent: str = Query(default=None),
    outcome: str = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = app_now().date()
    if not date_from:
        date_from = today.isoformat()
    if not date_to:
        date_to = today.isoformat()

    try:
        _date_from = date.fromisoformat(date_from)
    except ValueError:
        _date_from = today
    try:
        _date_to = date.fromisoformat(date_to)
    except ValueError:
        _date_to = today

    filters = [
        func.date(TelecallingRecord.call_date) >= _date_from,
        func.date(TelecallingRecord.call_date) <= _date_to,
    ]
    if agent:
        filters.append(TelecallingRecord.called_by == agent)
    elif current_user.role not in (UserRole.admin, UserRole.sales_manager):
        filters.append(TelecallingRecord.called_by == current_user.username)
    if outcome:
        filters.append(TelecallingRecord.call_outcome == outcome)

    result = await db.execute(
        select(TelecallingRecord).where(and_(*filters))
        .order_by(TelecallingRecord.call_date.desc())
    )
    records_list = result.scalars().all()

    return templates.TemplateResponse("telecalling/records.html", {
        "request": request,
        "current_user": current_user,
        "records": records_list,
        "date_from": date_from,
        "date_to": date_to,
        "agent": agent or "",
        "outcome": outcome or "",
        "today": today,
    })


@router.get("/agent-performance", response_class=HTMLResponse)
async def agent_performance(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-agent call stats combining DealerCall + TelecallingRecord tables."""
    from sqlalchemy import case as sa_case, union_all, literal_column, text as sa_text

    # ── DealerCall stats per agent ───────────────────────────────────────────
    dc_q = (
        select(
            DealerCall.called_by.label("agent"),
            func.count(DealerCall.id).label("total"),
            func.count(sa_case((DealerCall.call_outcome == "interested",     1))).label("interested"),
            func.count(sa_case((DealerCall.call_outcome == "callback",       1))).label("callback"),
            func.count(sa_case((DealerCall.call_outcome == "not_interested", 1))).label("not_interested"),
            func.count(sa_case((DealerCall.call_outcome == "no_answer",      1))).label("no_answer"),
            func.count(sa_case((DealerCall.call_outcome == "order_placed",   1))).label("order_placed"),
        )
        .group_by(DealerCall.called_by)
    )
    if current_user.role not in (UserRole.admin, UserRole.sales_manager):
        dc_q = dc_q.where(DealerCall.called_by == current_user.username)
    dc_rows = (await db.execute(dc_q)).all()

    # ── TelecallingRecord stats per agent ────────────────────────────────────
    tr_q = (
        select(
            TelecallingRecord.called_by.label("agent"),
            func.count(TelecallingRecord.id).label("total"),
            func.count(sa_case((TelecallingRecord.call_outcome == "interested",     1))).label("interested"),
            func.count(sa_case((TelecallingRecord.call_outcome == "callback",       1))).label("callback"),
            func.count(sa_case((TelecallingRecord.call_outcome == "not_interested", 1))).label("not_interested"),
            func.count(sa_case((TelecallingRecord.call_outcome == "no_answer",      1))).label("no_answer"),
            func.count(sa_case((TelecallingRecord.call_outcome == "order_placed",   1))).label("order_placed"),
        )
        .group_by(TelecallingRecord.called_by)
    )
    if current_user.role not in (UserRole.admin, UserRole.sales_manager):
        tr_q = tr_q.where(TelecallingRecord.called_by == current_user.username)
    tr_rows = (await db.execute(tr_q)).all()

    # ── Merge both result sets in Python ─────────────────────────────────────
    merged: dict = {}
    def _add(rows):
        for r in rows:
            agent = r.agent or "unknown"
            if agent not in merged:
                merged[agent] = {"agent": agent, "total": 0, "interested": 0,
                                 "callback": 0, "not_interested": 0,
                                 "no_answer": 0, "order_placed": 0}
            merged[agent]["total"]          += r.total
            merged[agent]["interested"]     += r.interested
            merged[agent]["callback"]       += r.callback
            merged[agent]["not_interested"] += r.not_interested
            merged[agent]["no_answer"]      += r.no_answer
            merged[agent]["order_placed"]   += r.order_placed
    _add(dc_rows)
    _add(tr_rows)

    # Compute conversion rate = (interested + order_placed) / total
    stats = []
    for d in sorted(merged.values(), key=lambda x: x["total"], reverse=True):
        conv = round((d["interested"] + d["order_placed"]) / d["total"] * 100, 1) if d["total"] else 0.0
        stats.append({**d, "conversion_rate": conv})

    return templates.TemplateResponse("telecalling/performance.html", {
        "request": request,
        "current_user": current_user,
        "stats": stats,
    })
