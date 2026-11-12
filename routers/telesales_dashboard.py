"""TeleSales Dashboard — Business Snapshot analysing Dealers, Device Requested
(TelecallerDispatchRequest), Sales List, and Returns. Moved out of the
Telecalling Dashboard into its own page."""
from templates_config import templates
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case as sa_case
from sqlalchemy.orm import selectinload
from database import get_db
from models.dealers import Dealer
from models.dispatch_request import TelecallerDispatchRequest
from models.dealer_quotation import DealerQuotation
from models.sales import Sale, Return
from models.user import User, UserRole
from auth.dependencies import get_current_user, require_module_perm

router = APIRouter(prefix="/telesales-dashboard", tags=["telesales-dashboard"])


@router.get("", response_class=HTMLResponse)
async def telesales_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _perm: User = Depends(require_module_perm("telesales_dashboard", "enable")),
):
    today = app_now().date()

    # Admin + Sales Manager see the whole business; everyone else sees only
    # data tied to their own username (their dealers, their requests, their sales).
    is_manager_view = current_user.role in (UserRole.admin, UserRole.sales_manager)
    month_start = today.replace(day=1)

    # Dealers
    dealer_q = select(func.count(Dealer.id))
    active_dealer_q = select(func.count(Dealer.id)).where(Dealer.status == "active")
    if not is_manager_view:
        dealer_q = dealer_q.where(Dealer.assigned_to == current_user.username)
        active_dealer_q = active_dealer_q.where(Dealer.assigned_to == current_user.username)
    dealer_stats = {
        "total": int((await db.execute(dealer_q)).scalar() or 0),
        "active": int((await db.execute(active_dealer_q)).scalar() or 0),
    }

    # Device Requested (TelecallerDispatchRequest — raised from Ready to Sale)
    dr_stmt = select(
        func.count(TelecallerDispatchRequest.id),
        func.count(sa_case((TelecallerDispatchRequest.status == "requested", 1))),
        func.count(sa_case((TelecallerDispatchRequest.status == "approved", 1))),
    )
    if not is_manager_view:
        dr_stmt = dr_stmt.where(TelecallerDispatchRequest.telecaller_username == current_user.username)
    dr_row = (await db.execute(dr_stmt)).one()
    device_request_stats = {
        "total":    int(dr_row[0] or 0),
        "pending":  int(dr_row[1] or 0),
        "approved": int(dr_row[2] or 0),
    }
    dr_list_stmt = (
        select(TelecallerDispatchRequest)
        .options(selectinload(TelecallerDispatchRequest.device))
        .order_by(TelecallerDispatchRequest.created_at.desc())
        .limit(8)
    )
    if not is_manager_view:
        dr_list_stmt = dr_list_stmt.where(TelecallerDispatchRequest.telecaller_username == current_user.username)
    recent_device_requests = (await db.execute(dr_list_stmt)).scalars().all()

    # Sales List (today + month-to-date)
    sales_stmt = select(
        func.count(sa_case((func.date(Sale.sold_at) == today, 1))),
        func.coalesce(func.sum(sa_case((func.date(Sale.sold_at) == today, Sale.sale_price), else_=0)), 0),
        func.count(sa_case((Sale.sold_at >= month_start, 1))),
        func.coalesce(func.sum(sa_case((Sale.sold_at >= month_start, Sale.sale_price), else_=0)), 0),
    )
    if not is_manager_view:
        sales_stmt = sales_stmt.where(Sale.sold_by == current_user.username)
    sales_row = (await db.execute(sales_stmt)).one()
    sales_stats = {
        "today_count":   int(sales_row[0] or 0),
        "today_revenue": float(sales_row[1] or 0),
        "month_count":   int(sales_row[2] or 0),
        "month_revenue": float(sales_row[3] or 0),
    }
    recent_sales_stmt = select(Sale).order_by(Sale.sold_at.desc()).limit(8)
    if not is_manager_view:
        recent_sales_stmt = recent_sales_stmt.where(Sale.sold_by == current_user.username)
    recent_sales = (await db.execute(recent_sales_stmt)).scalars().all()

    # Returns (month-to-date, tied back to the agent via Sale.sold_by)
    returns_count_stmt = (
        select(func.count(Return.id))
        .join(Sale, Return.sale_id == Sale.id)
        .where(Return.return_date >= month_start)
    )
    if not is_manager_view:
        returns_count_stmt = returns_count_stmt.where(Sale.sold_by == current_user.username)
    returns_month_count = int((await db.execute(returns_count_stmt)).scalar() or 0)

    recent_returns_stmt = (
        select(Return, Sale)
        .join(Sale, Return.sale_id == Sale.id)
        .order_by(Return.return_date.desc())
        .limit(8)
    )
    if not is_manager_view:
        recent_returns_stmt = recent_returns_stmt.where(Sale.sold_by == current_user.username)
    recent_returns = (await db.execute(recent_returns_stmt)).all()

    # Quotation Shared — most recent dealer quotations (read-only here; the
    # Confirm/Delete actions live on Dealer Management / Dealer Detail)
    quotations_stmt = (
        select(DealerQuotation)
        .join(Dealer, DealerQuotation.dealer_id == Dealer.id)
        .order_by(DealerQuotation.created_at.desc())
        .limit(20)
    )
    if not is_manager_view:
        quotations_stmt = quotations_stmt.where(Dealer.assigned_to == current_user.username)
    recent_quotations = (await db.execute(quotations_stmt)).scalars().all()

    # Telecalling Quotations waiting for Level 1 approval (2026-11-10):
    # Added / Negotiation quotations raised by someone below this user in
    # Report To (any depth); admin sees all of them.
    from services.report_chain import subordinates
    _role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    pending_stmt = (select(DealerQuotation)
                    .where(DealerQuotation.status.in_(["added", "negotiation"]))
                    .order_by(DealerQuotation.created_at.desc()))
    if _role != "admin":
        _below = await subordinates(db, current_user.username)
        pending_stmt = pending_stmt.where(DealerQuotation.created_by.in_(_below or {"__none__"}))
    pending_quotations = (await db.execute(pending_stmt)).scalars().all()

    # ── Sales analysis (2026-11-12, Pankaj — layout from "Telecaller
    # Dashboard"): one month of sales, sent to the page as compact rows that
    # the page filters (Salesperson / Stock type / Brand) and charts itself.
    # Who the sales analysis covers (2026-11-12, Pankaj): admin → everyone; a
    # Group Config manager → only their team (every level down, plus
    # themselves); otherwise Sales Manager → everyone, anyone else → their own.
    from utils.attendance_groups import is_group_manager, managed_usernames
    _rv = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    team = None
    if _rv != "admin" and await is_group_manager(db, current_user.username):
        team = set(await managed_usernames(db, current_user.username)) | {current_user.username}
    elif not is_manager_view:
        team = {current_user.username}
    sales_data, month_options, month_sel = await _sales_month_data(
        db, request.query_params.get("month", ""), today, team,
        with_price=can_pricing(current_user))
    sales_scope = ("all" if team is None else "team" if len(team) > 1 else "own")

    return templates.TemplateResponse("telesales/dashboard.html", {
        "sales_data": sales_data, "month_options": month_options, "month_sel": month_sel,
        "sales_scope": sales_scope,
        "request": request,
        "current_user": current_user,
        "is_manager_view": is_manager_view,
        "dealer_stats": dealer_stats,
        "device_request_stats": device_request_stats,
        "recent_device_requests": recent_device_requests,
        "sales_stats": sales_stats,
        "recent_sales": recent_sales,
        "returns_month_count": returns_month_count,
        "recent_returns": recent_returns,
        "recent_quotations": recent_quotations,
        "pending_quotations": pending_quotations,
    })


def can_pricing(user) -> bool:
    from templates_config import templates as _t
    try:
        return bool(_t.env.globals["can"](user, "telesales_dashboard", "pricing"))
    except Exception:
        return False


# Stock type of a sold tag: As-is lot = it was sold from an As-Is sub-lot;
# Ready = a refurbished computer; Other = monitors, accessories and the rest.
_COMPUTER_TYPES = {"laptop", "desktop", "mini pc", "workstation", "all in one", "aio", "tablet", "server"}


def _stock_type(d) -> str:
    if (d.sub_lot_number or "").strip():
        return "As-is lot"
    return "Ready" if (d.device_type or "").strip().lower() in _COMPUTER_TYPES else "Other"


def _ram_bucket(gb) -> str:
    if not gb:
        return "None / —"
    for b in (32, 16, 8, 4):
        if gb >= b:
            return f"{b} GB" if b < 32 else "32 GB+"
    return "Below 4 GB"


def _ssd_bucket(d) -> str:
    gb = d.storage_gb or d.hdd_capacity_gb or 0
    if gb in (1, 2):          # entered as TB
        gb *= 1024
    if not gb:
        return "None"
    if gb >= 512:
        return "512 GB+"
    if gb >= 256:
        return "256 GB"
    if gb >= 128:
        return "128 GB"
    return "Below 128 GB"


def _cpu_label(d) -> str:
    c = (d.cpu or "").strip()
    if not c:
        return "Unlisted"
    g = (d.generation or "").strip()
    return f"{c.upper()} · {g.upper()}" if g else c.upper()


async def _sales_month_data(db, month: str, today, team, with_price: bool):
    """Rows for one calendar month: [day, invoice, customer, salesperson,
    brand, model, category, stock type, cpu, ram, storage, price] with every
    text column as an index into its own list (like the source dashboard)."""
    from datetime import date, datetime as _dt
    from models.device import Device
    try:
        y, m = (int(x) for x in month.split("-"))
        start = date(y, m, 1)
    except Exception:
        start = today.replace(day=1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    # Month picker: the last 12 months.
    opts, cur = [], today.replace(day=1)
    for _ in range(12):
        opts.append((cur.strftime("%Y-%m"), cur.strftime("%B %Y")))
        cur = date(cur.year - (cur.month == 1), (cur.month - 2) % 12 + 1, 1)

    q = (select(Sale, Device).join(Device, Sale.device_id == Device.id)
         .where(Sale.sold_at >= _dt.combine(start, _dt.min.time()),
                Sale.sold_at < _dt.combine(end, _dt.min.time()))
         .order_by(Sale.sold_at))
    if team is not None:
        # The team's sales: credited to one of them as Sales Person, or (when
        # no Sales Person is linked) recorded by one of them.
        from sqlalchemy import or_, and_
        q = q.where(or_(Sale.sales_person_username.in_(team),
                        and_(Sale.sales_person_username.is_(None), Sale.sold_by.in_(team))))
    rows = (await db.execute(q)).all()

    urows = (await db.execute(select(User.username, User.full_name, User.reports_to))).all()
    names = {u: (f or u) for u, f, _ in urows}
    boss = {u: r for u, _, r in urows if r}

    def depth(u):
        """Levels above this user in Report To (0 = top of the tree)."""
        n, seen = 0, {u}
        while boss.get(u) and boss[u] not in seen and n < 15:
            u = boss[u]; seen.add(u); n += 1
        return n
    cols = ["inv", "party", "rep", "make", "model", "cat", "mat", "cpu", "ram", "ssd"]
    dicts = {c: [] for c in cols}
    index = {c: {} for c in cols}

    def ix(c, v):
        v = (v or "").strip() or "—"
        i = index[c].get(v)
        if i is None:
            i = index[c][v] = len(dicts[c])
            dicts[c].append(v)
        return i

    out, rep_user = [], {}
    for sale, d in rows:
        # Salesperson: the linked user (Sales Person picked on the sale, or
        # linked on the Sales report), else the typed name, else who recorded it.
        if sale.sales_person_username:
            rep = names.get(sale.sales_person_username, sale.sales_person_username)
            rep_user.setdefault(rep, sale.sales_person_username)
        elif (sale.sales_person or "").strip():
            rep = sale.sales_person.strip()
        else:
            rep = names.get(sale.sold_by or "", sale.sold_by or "") or "Unassigned"
            if sale.sold_by in names:
                rep_user.setdefault(rep, sale.sold_by)
        out.append([
            sale.sold_at.day,
            ix("inv", (sale.invoice_no or "").strip() or sale.sale_number),
            ix("party", " ".join((sale.customer_name or "").split()) or "Walk-in"),
            ix("rep", rep),
            ix("make", (d.brand or "").strip() or "Unknown"),
            ix("model", (d.model or "").strip() or "Unknown"),
            ix("cat", (d.device_type or "").strip() or "Other"),
            ix("mat", _stock_type(d)),
            ix("cpu", _cpu_label(d)),
            ix("ram", _ram_bucket(d.ram_gb)),
            ix("ssd", _ssd_bucket(d)),
            round(float(sale.sale_price or 0), 2) if with_price else 0,
        ])
    # Manager column (Report To) and hierarchy order for Salesperson
    # performance: [manager name, depth of the salesperson]; a typed name
    # that isn't linked to a user has no manager and sorts last.
    rep_info = []
    for rep in dicts["rep"]:
        u = rep_user.get(rep)
        if u:
            m = boss.get(u)
            rep_info.append([names.get(m, m) if m else "", depth(u)])
        else:
            rep_info.append(["", 99])
    days_in_month = (end - start).days
    return ({"cols": ["day"] + cols + ["price"], "dicts": dicts, "rows": out, "rep_info": rep_info,
             "price": with_price, "year": start.year, "month": start.month,
             "days": days_in_month, "label": start.strftime("%B %Y")},
            opts, start.strftime("%Y-%m"))
