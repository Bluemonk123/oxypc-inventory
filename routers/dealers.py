from templates_config import templates
from datetime import datetime, date
from utils.timezone import app_now
import csv
import io
import json
import math
from html import escape as esc
from decimal import Decimal
from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, Integer, delete as sa_delete, text as sa_text
from sqlalchemy.orm import selectinload
from database import get_db
from utils.csv_decode import decode_csv_bytes
from utils.dealer_code import next_dealer_code, dealer_code_allocator
from utils.call_outcomes import (
    OUTCOME_LABELS, normalize_outcome as _norm_outcome, tally as _tally_outcomes,
    interested_total as _interested_total,
)
from models.dealers import Dealer, DealerCall, DealerAssignment, DealerOrder, DealerCreditNote
from models.user import User, UserRole
from models.master import MasterData
from utils.master_data import master_values, add_missing_master_values, refresh_master_cache
from utils.dealer_fields import normalize_state, parse_google_cid, safe_external_url
from auth.dependencies import (
    get_current_user, require_roles, verify_csrf, require_module_perm,
    _matrix_grants_path,
)
from utils.attendance_groups import is_group_manager, managed_usernames
from models.crm import CustomerReceipt
from models.dealer_quotation import DealerQuotation
from routers.company_settings import get_company_settings
from services.audit_engine import audit

# CSRF is enforced at the router, not per endpoint. Nine of the fourteen POSTs
# here were unprotected — creating and editing dealers, logging calls, raising
# orders, recording payments and the CSV bulk upload — because the check was
# opt-in and easy to forget on a new route. A router-level dependency closes the
# whole surface and makes the next endpoint secure by default.
# verify_csrf is a no-op for GET/HEAD/OPTIONS, so read routes are unaffected.
router = APIRouter(prefix="/dealers", tags=["dealers"],
                   dependencies=[Depends(verify_csrf)])

# Telecalling deal-detail dropdown categories — admin-managed via /admin/master
# (Telecalling accordion section). Keys map 1:1 to DealerCall form field names.
TC_FIELD_CATEGORIES = {
    "category":      "tc_category",
    "product_model": "tc_model",
    "configuration": "tc_configuration",
    "deal_status":   "tc_deal_status",
    "whom_to_sell":  "tc_whom_to_sell",
    "deals_in":      "tc_deals_in",
    "stock_type":    "tc_stock_type",
}


async def _tc_field_options(db: AsyncSession) -> dict:
    """Fetch active master-data options for every telecalling dropdown field."""
    result = await db.execute(
        select(MasterData)
        .where(MasterData.category.in_(TC_FIELD_CATEGORIES.values()), MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )
    rows = result.scalars().all()
    by_category = {}
    for r in rows:
        by_category.setdefault(r.category, []).append(r.value)
    return {field: by_category.get(cat, []) for field, cat in TC_FIELD_CATEGORIES.items()}

SALES_ROLES = (UserRole.admin, UserRole.sales, UserRole.sales_manager, UserRole.telecaller)

# FastAPI dependency — use in route signatures instead of manual role checks
require_sales = require_roles(*SALES_ROLES)
require_sales_mgr = require_roles(UserRole.admin, UserRole.sales_manager)


async def _can_manage_dealers(db: AsyncSession, user: User, path: str = "") -> bool:
    """Whether this user may act on other people's dealer records — the Bulk
    Delete control and the Assigned-To filter.

    Admin and Sales Manager qualify by role. A user set as manager of an
    Attendance Group (Application Settings -> Attendance Config) also qualifies:
    that is the only place the app records who supervises whom, so it is what
    "manager" means here too.

    The last two clauses mirror require_roles() so this gate does not quietly
    revoke access that dependency already grants — custom roles pass any
    non-admin-only gate, and an explicit Module Permission grant wins.
    """
    role_val = getattr(user.role, "value", None) or str(user.role)
    if role_val in ("admin", "sales_manager"):
        return True
    if await is_group_manager(db, user.username):
        return True
    if role_val not in {r.value for r in UserRole}:
        return True
    return bool(path) and _matrix_grants_path(role_val, path)


async def require_dealer_manager(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User:
    """Route gate matching _can_manage_dealers, so the Bulk Delete button and
    the endpoint behind it can never disagree about who is allowed."""
    if await _can_manage_dealers(db, current_user, request.url.path):
        return current_user
    from fastapi import HTTPException
    raise HTTPException(status_code=403, detail="Access denied")
OUTSTANDING_STATUSES = ("pending", "confirmed", "delivered")
PER_PAGE = 25


def _ranked_calls_subq(ids_subq):
    """Latest call (rn == 1) per dealer_id, restricted to the given dealer-id subquery."""
    rn_col = func.row_number().over(
        partition_by=DealerCall.dealer_id,
        order_by=DealerCall.call_date.desc()
    ).label("rn")
    return select(
        DealerCall.dealer_id,
        DealerCall.call_outcome,
        DealerCall.next_followup_date,
        rn_col,
    ).where(DealerCall.dealer_id.in_(ids_subq)).subquery()


def _combine_date_time(date_str: str | None, time_str: str | None) -> datetime | None:
    """Combine a 'YYYY-MM-DD' date field and an 'HH:MM' time field into a
    datetime. Defaults to 00:00 if no time given, or midnight if only a
    date string was submitted (also handles a plain ISO datetime string)."""
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()
    time_str = (time_str or "").strip() or "00:00"
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            return datetime.fromisoformat(date_str)
        except ValueError:
            return None


def _upsell_suggestions(dealer: Dealer, outstanding_live: float = 0.0) -> list:
    suggestions = []
    days_since_sale = None
    if dealer.last_sale_date:
        days_since_sale = (app_now() - dealer.last_sale_date).days
    if days_since_sale and days_since_sale > 30:
        suggestions.append(f"No purchase in {days_since_sale} days — good time to reconnect with new stock offers")
    if outstanding_live > 0:
        suggestions.append(f"Outstanding ₹{int(outstanding_live):,} — can offer credit extension for new order")
    if dealer.preferred_categories:
        suggestions.append(f"Prefers: {dealer.preferred_categories} — share availability updates")
    if not dealer.last_sale_date:
        suggestions.append("New dealer — introduce full product catalog")
    return suggestions


async def _recent_quotations(db: AsyncSession) -> list:
    """All Dealer Quotations across all dealers, for the "Quotation Shared"
    table on the Dealer Management page (client-side DataTables pagination)."""
    result = await db.execute(
        select(DealerQuotation).order_by(DealerQuotation.created_at.desc())
    )
    return result.scalars().all()


def _dealer_text_search(query, text: str):
    """Free-text match used by the page's Search filter and by the Dealers
    table's own search box (both may apply at once)."""
    text = (text or "").strip()
    if not text:
        return query
    from sqlalchemy import or_
    like = f"%{text}%"
    return query.where(or_(
        Dealer.business_name.ilike(like),
        Dealer.dealer_code.ilike(like),
        Dealer.city.ilike(like),
        Dealer.address.ilike(like),
        Dealer.phone.ilike(like),
        Dealer.email.ilike(like),
        Dealer.contact_person.ilike(like),
        Dealer.dealer_sub_category.ilike(like),
        Dealer.state.ilike(like),
    ))


SUB_CATEGORY_NOT_SET = "__none__"


def _sub_category_filter(query, sub_category: str):
    """Dealer Sub Category filter shared by Dealer Management and Assign Dealer
    Leads; "__none__" picks dealers with no Sub Category."""
    if sub_category == SUB_CATEGORY_NOT_SET:
        from sqlalchemy import or_
        return query.where(or_(Dealer.dealer_sub_category.is_(None), Dealer.dealer_sub_category == ""))
    if sub_category:
        return query.where(Dealer.dealer_sub_category == sub_category)
    return query


def _dealer_base_query(
    current_user: User,
    q: str = "", status: str = "", assigned: str = "", city: str = "",
    calling_date_from: str = "", calling_date_to: str = "",
    filter_qty: str = "", filter_purchase_qty: str = "", filter_sale_qty: str = "",
    filter_whom_to_sell: str = "", filter_deals_in: str = "",
    followup_from: str = "", followup_to: str = "",
    dealer_type: str = "", sub_category: str = "",
):
    """Builds the filtered Dealer select — shared by the page (for its count
    cards) and /dealers/data (for the paginated rows), so the two can never
    disagree about which dealers match the current filters."""
    base_query = select(Dealer).where(Dealer.trashed_at.is_(None))  # exclude soft-deleted
    base_query = _dealer_text_search(base_query, q)
    if status:
        base_query = base_query.where(Dealer.status == status)
    if assigned:
        base_query = base_query.where(Dealer.assigned_to == assigned)
    elif current_user.role == UserRole.telecaller:
        # Sourcing Sales (UserRole.sales) sees every user's dealers, same as
        # admin/sales_manager — only Telecaller stays scoped to their own.
        base_query = base_query.where(Dealer.assigned_to == current_user.username)
    if city:
        base_query = base_query.where(Dealer.city.ilike(f"%{city}%"))
    if dealer_type:
        base_query = base_query.where(Dealer.dealer_type == dealer_type)
    base_query = _sub_category_filter(base_query, sub_category)

    # Latest-call-derived filters (calling date range, quantities, whom-to-sell,
    # deals_in) — all scoped to each dealer's MOST RECENT call, same "rn == 1"
    # window-function pattern used for the recent_call_map / followup_count.
    has_latest_call_filter = bool(
        calling_date_from or calling_date_to
        or filter_qty.strip().isdigit() or filter_purchase_qty.strip().isdigit()
        or filter_sale_qty.strip().isdigit() or filter_whom_to_sell or filter_deals_in
    )

    if has_latest_call_filter:
        # "Latest call per dealer matches all filters" via a ranked subquery
        # (rn == 1), same technique as _ranked_calls_subq.
        latest_call_rn = func.row_number().over(
            partition_by=DealerCall.dealer_id,
            order_by=DealerCall.call_date.desc()
        ).label("rn")
        ranked_inner = select(
            DealerCall.dealer_id, DealerCall.call_date, DealerCall.qty,
            DealerCall.purchase_quantity, DealerCall.sale_quantity,
            DealerCall.whom_to_sell, DealerCall.deals_in,
            latest_call_rn,
        ).subquery()
        ranked_filters = [ranked_inner.c.rn == 1]
        if calling_date_from:
            try:
                ranked_filters.append(ranked_inner.c.call_date >= datetime.strptime(calling_date_from, "%Y-%m-%d"))
            except ValueError:
                pass
        if calling_date_to:
            try:
                ranked_filters.append(ranked_inner.c.call_date <= datetime.strptime(calling_date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
            except ValueError:
                pass
        if filter_qty.strip().isdigit():
            ranked_filters.append(ranked_inner.c.qty == int(filter_qty))
        if filter_purchase_qty.strip().isdigit():
            ranked_filters.append(ranked_inner.c.purchase_quantity == int(filter_purchase_qty))
        if filter_sale_qty.strip().isdigit():
            ranked_filters.append(ranked_inner.c.sale_quantity == int(filter_sale_qty))
        if filter_whom_to_sell:
            ranked_filters.append(ranked_inner.c.whom_to_sell == filter_whom_to_sell)
        if filter_deals_in:
            ranked_filters.append(ranked_inner.c.deals_in == filter_deals_in)
        matching_dealer_ids = select(ranked_inner.c.dealer_id).where(*ranked_filters)
        base_query = base_query.where(Dealer.id.in_(matching_dealer_ids))

    # Followup date filter — filter dealers who have a DealerCall with
    # next_followup_date in the given range
    if followup_from or followup_to:
        fu_subq = select(DealerCall.dealer_id).distinct()
        if followup_from:
            try:
                fu_subq = fu_subq.where(
                    DealerCall.next_followup_date >= datetime.strptime(followup_from, "%Y-%m-%d")
                )
            except ValueError:
                pass
        if followup_to:
            try:
                fu_subq = fu_subq.where(
                    DealerCall.next_followup_date <= datetime.strptime(followup_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                )
            except ValueError:
                pass
        base_query = base_query.where(Dealer.id.in_(fu_subq))

    return base_query


async def _recent_call_map(db: AsyncSession, dealer_ids: list) -> dict:
    """Most recent call outcome + items per dealer (for pills), scoped to the
    given dealer_ids — the page's count cards and /dealers/data's current
    page of rows call this with different id sets."""
    recent_call_map: dict = {}
    if dealer_ids:
        rn_col = func.row_number().over(
            partition_by=DealerCall.dealer_id,
            order_by=DealerCall.call_date.desc()
        ).label("rn")
        inner = select(
            DealerCall.id,
            DealerCall.dealer_id,
            DealerCall.call_outcome,
            DealerCall.items_discussed,
            DealerCall.next_followup_date,
            DealerCall.call_date,
            DealerCall.qty,
            DealerCall.purchase_quantity,
            DealerCall.calling_remark,
            DealerCall.notes,
            DealerCall.category,
            DealerCall.sale_quantity,
            DealerCall.deals_in,
            DealerCall.whom_to_sell,
            DealerCall.call_mode,
            DealerCall.call_type,
            rn_col,
        ).where(DealerCall.dealer_id.in_(dealer_ids)).subquery()
        rc_rows = (await db.execute(
            select(
                inner.c.id, inner.c.dealer_id, inner.c.call_outcome, inner.c.items_discussed,
                inner.c.next_followup_date, inner.c.call_date, inner.c.qty, inner.c.purchase_quantity,
                inner.c.calling_remark, inner.c.notes, inner.c.category, inner.c.sale_quantity,
                inner.c.deals_in, inner.c.whom_to_sell, inner.c.call_mode, inner.c.call_type,
            ).where(inner.c.rn == 1)
        )).all()
        recent_call_map = {
            str(r.dealer_id): {
                "call_id": str(r.id),
                # Canonical key so the Status badge and the outcome cards above
                # it can never disagree about what the same call was.
                "outcome": _norm_outcome(r.call_outcome),
                "items_text": r.items_discussed or "",
                "next_followup_date": r.next_followup_date,
                "call_date": r.call_date,
                "qty": r.qty,
                "purchase_quantity": r.purchase_quantity,
                "calling_remark": r.calling_remark or "",
                "notes": r.notes or "",
                "category": r.category or "",
                "sale_quantity": r.sale_quantity,
                "deals_in": r.deals_in or "",
                "whom_to_sell": r.whom_to_sell or "",
                "call_mode": r.call_mode or "",
                "call_type": r.call_type or "",
            }
            for r in rc_rows
        }
    return recent_call_map


async def _assignee_name_map(db: AsyncSession, dealers: list) -> dict:
    """Username -> full name, for the "Assigned" column (built regardless of
    viewer role — display-only, not a permission surface)."""
    assignee_usernames = {d.assigned_to for d in dealers if d.assigned_to}
    if not assignee_usernames:
        return {}
    au_result = await db.execute(
        select(User.username, User.full_name).where(User.username.in_(assignee_usernames))
    )
    return {row.username: row.full_name for row in au_result.all()}


@router.get("", response_class=HTMLResponse)
async def list_dealers(
    request: Request,
    q: str = Query(default=""),
    status: str = Query(default=""),
    assigned: str = Query(default=""),
    city: str = Query(default=""),
    calling_date_from: str = Query(default=""),
    calling_date_to: str = Query(default=""),
    filter_qty: str = Query(default=""),
    filter_purchase_qty: str = Query(default=""),
    filter_sale_qty: str = Query(default=""),
    filter_whom_to_sell: str = Query(default=""),
    filter_deals_in: str = Query(default=""),
    followup_from: str = Query(default=""),
    followup_to: str = Query(default=""),
    dealer_type: str = Query(default=""),
    sub_category: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    base_query = _dealer_base_query(
        current_user, q, status, assigned, city, calling_date_from, calling_date_to,
        filter_qty, filter_purchase_qty, filter_sale_qty, filter_whom_to_sell,
        filter_deals_in, followup_from, followup_to, dealer_type, sub_category,
    )

    # Total count
    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total_count = count_result.scalar() or 0

    # Active count
    active_q = base_query.where(Dealer.status == "active")
    active_result = await db.execute(select(func.count()).select_from(active_q.subquery()))
    active_count = active_result.scalar() or 0

    # Subquery of filtered dealer IDs — shared by followup_count, outstanding, outcome_stats.
    # Select the id OFF THE SUBQUERY, not off Dealer. `select(Dealer.id)
    # .select_from(base_query.subquery())` compiles to
    # "FROM (…filtered…) AS anon_1, dealers" — an implicit cross join whose
    # result is every dealer id in the table, so the cards silently ignored the
    # page's filters and the trashed-dealer exclusion.
    _filtered_dealers = base_query.subquery()
    filtered_ids_subq = select(_filtered_dealers.c.id)

    today = app_now().date()

    # ── Outcome distribution (respects the list's own filters, all roles) ────
    ranked_calls_inner = _ranked_calls_subq(filtered_ids_subq)
    outcome_rows = (await db.execute(
        select(ranked_calls_inner.c.call_outcome, func.count().label("cnt"))
        .where(ranked_calls_inner.c.rn == 1)
        .group_by(ranked_calls_inner.c.call_outcome)
    )).all()
    # Fold spelling variants together — the imported rows say "not connected"
    # where the in-app form says "no_answer", and both must hit the same card.
    outcome_stats: dict = _tally_outcomes((row.call_outcome, row.cnt) for row in outcome_rows)
    # Card view of the same tally: Interested absorbs Detail Sent. Kept as a
    # separate dict so outcome_stats stays a faithful per-outcome breakdown for
    # anything else that reads it.
    card_stats = dict(outcome_stats)
    card_stats["interested"] = _interested_total(outcome_stats)

    # ── Follow-up counts — scoped to the logged-in user's own dealers unless
    # admin/Sourcing Sales (see the count across all dealers, ignoring assignment) ───
    if current_user.role in (UserRole.admin, UserRole.sales):
        followup_scope_ids = select(Dealer.id)
    else:
        followup_scope_ids = select(Dealer.id).where(Dealer.assigned_to == current_user.username)
    followup_ranked = _ranked_calls_subq(followup_scope_ids)

    # followup_count: dealers whose LATEST call outcome is 'followup'
    # AND whose next_followup_date is today or already past (i.e. due / overdue)
    fu_result = await db.execute(
        select(func.count())
        .select_from(followup_ranked)
        .where(
            followup_ranked.c.rn == 1,
            followup_ranked.c.call_outcome == 'followup',
            func.date(followup_ranked.c.next_followup_date) <= today,
        )
    )
    followup_count = int(fu_result.scalar() or 0)

    # today_followup_count: dealers whose LATEST call has a next_followup_date
    # scheduled for exactly today (regardless of outcome)
    today_fu_result = await db.execute(
        select(func.count())
        .select_from(followup_ranked)
        .where(
            followup_ranked.c.rn == 1,
            func.date(followup_ranked.c.next_followup_date) == today,
        )
    )
    today_followup_count = int(today_fu_result.scalar() or 0)
    # ──────────────────────────────────────────────────────────────────────────

    # Who may act on other people's dealer records — drives both the Bulk Delete
    # control and the Assigned-To filter, and matches require_dealer_manager so
    # the button and its endpoint agree.
    can_manage_dealers = await _can_manage_dealers(db, current_user, request.url.path)

    # Assigned-To filter dropdown. Admin and Sourcing Sales see every sales
    # executive. An Attendance Group manager additionally sees their own group
    # members — added by union rather than as an else-branch, because a manager
    # often already holds a sales role and would otherwise get the generic list
    # with their own team missing from it (group members are frequently not
    # sales-role users at all).
    role_user_ids: list = []
    if current_user.role in (UserRole.admin, UserRole.sales):
        role_user_ids = (await db.execute(
            select(User.username).where(
                User.role.in_([UserRole.sales, UserRole.sales_manager, UserRole.telecaller]),
                User.status == True,
            )
        )).scalars().all()
    member_names = await managed_usernames(db, current_user.username)
    wanted = set(role_user_ids) | set(member_names)
    sales_users: list = []
    if wanted:
        sales_users = (await db.execute(
            select(User).where(User.username.in_(wanted), User.status == True)
            .order_by(User.full_name)
        )).scalars().all()

    # Outstanding total — scoped to filtered dealers
    out_total_result = await db.execute(
        select(func.coalesce(func.sum(DealerOrder.due_amount), 0))
        .where(
            DealerOrder.dealer_id.in_(filtered_ids_subq),
            DealerOrder.status.in_(OUTSTANDING_STATUSES),
        )
    )
    outstanding = round(float(out_total_result.scalar() or 0))
    tc_options = await _tc_field_options(db)

    return templates.TemplateResponse("dealers/list.html", {
        "request": request,
        "current_user": current_user,
        "q": q,
        "status": status,
        "assigned": assigned,
        "city": city,
        "calling_date_from": calling_date_from,
        "calling_date_to": calling_date_to,
        "filter_qty": filter_qty,
        "filter_purchase_qty": filter_purchase_qty,
        "filter_sale_qty": filter_sale_qty,
        "filter_whom_to_sell": filter_whom_to_sell,
        "filter_deals_in": filter_deals_in,
        "followup_from": followup_from,
        "followup_to": followup_to,
        "dealer_type": dealer_type,
        "sub_category": sub_category,
        "dealer_type_options": await master_values(db, "dealer_dealer_type"),
        "total_count": total_count,
        "active_count": active_count,
        "followup_count": followup_count,
        "today_followup_count": today_followup_count,
        "outstanding": f"{outstanding:,}",
        "outcome_stats": card_stats,
        "outcome_labels": OUTCOME_LABELS,
        "can_manage_dealers": can_manage_dealers,
        "sales_users": sales_users,
        "per_page": PER_PAGE,
        "whom_to_sell_options": tc_options["whom_to_sell"],
        "deals_in_options": tc_options["deals_in"],
        "call_mode_options": await master_values(db, "call_mode"),
        "call_type_options": await master_values(db, "call_type"),
        "quotations": await _recent_quotations(db),
        "company_settings": await get_company_settings(db),
    })


_OC_COLORS = {
    "interested": "success", "not_interested": "danger", "details_sent": "info",
    "callback": "warning", "not_connected": "secondary", "no_requirement": "dark",
    "language_barrier": "dark", "busy": "secondary", "order_placed": "primary",
    "followup": "info",
}


def _truncate(s: str, n: int = 30) -> str:
    s = s or ""
    return (s[:n] + "…") if len(s) > n else s


@router.get("/data")
async def dealers_data(
    request: Request,
    draw: int = Query(1), start: int = Query(0), length: int = Query(25),
    q: str = Query(default=""), status: str = Query(default=""),
    assigned: str = Query(default=""), city: str = Query(default=""),
    calling_date_from: str = Query(default=""), calling_date_to: str = Query(default=""),
    filter_qty: str = Query(default=""), filter_purchase_qty: str = Query(default=""),
    filter_sale_qty: str = Query(default=""), filter_whom_to_sell: str = Query(default=""),
    filter_deals_in: str = Query(default=""),
    followup_from: str = Query(default=""), followup_to: str = Query(default=""),
    dealer_type: str = Query(default=""), sub_category: str = Query(default=""),
    search_value: str = Query(default="", alias="search[value]"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """DataTables server-side feed for the Dealers list — same filter set as
    the page itself (see _dealer_base_query), fed the current page's slice
    plus that page's recent-call/assignee lookups."""
    base_query = _dealer_base_query(
        current_user, q, status, assigned, city, calling_date_from, calling_date_to,
        filter_qty, filter_purchase_qty, filter_sale_qty, filter_whom_to_sell,
        filter_deals_in, followup_from, followup_to, dealer_type, sub_category,
    )
    # The table's own search box narrows the page's filtered set further.
    base_query = _dealer_text_search(base_query, search_value)
    # recordsTotal ignores every filter but the mandatory role scope — reuse
    # the same builder with no filter args so the two queries can't drift.
    unfiltered_query = _dealer_base_query(current_user)

    total = (await db.execute(
        select(func.count()).select_from(unfiltered_query.subquery())
    )).scalar() or 0
    filtered = (await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )).scalar() or 0

    # Newest added first (same default as Assign Dealer Leads); dealer_code
    # breaks ties so rows inserted in the same upload keep a stable order
    # across pages.
    dealers = (await db.execute(
        base_query.order_by(Dealer.created_at.desc().nulls_last(), Dealer.dealer_code.desc())
        .offset(start).limit(length)
    )).scalars().all()
    dealer_ids = [d.id for d in dealers]

    recent_call_map = await _recent_call_map(db, dealer_ids)
    assignee_name_map = await _assignee_name_map(db, dealers)
    can_manage_dealers = await _can_manage_dealers(db, current_user, "/dealers")
    today = app_now().date()

    data = []
    for d in dealers:
        rc = recent_call_map.get(str(d.id), {})
        outcome = rc.get("outcome", "")
        call_date = rc.get("call_date")
        next_fu = rc.get("next_followup_date")
        row_data = {
            "id": str(d.id), "dealer_code": d.dealer_code, "business_name": d.business_name,
            "dealer_type": d.dealer_type, "dealer_sub_category": d.dealer_sub_category or "",
            "contact_person": d.contact_person or "",
            "phone": d.phone or "", "email": d.email or "", "address": d.address or "",
            "city": d.city or "", "state": d.state or "",
            "website": d.website or "", "google_cid": d.google_cid or "",
            "gstin": d.gstin or "", "status": d.status, "call_id": rc.get("call_id", ""),
            "call_date": call_date.strftime("%d-%m-%Y %H:%M") if call_date else "",
            "qty": rc.get("qty"), "items_discussed": rc.get("items_text", ""),
            "calling_remark": rc.get("calling_remark", ""), "notes": rc.get("notes", ""),
            "purchase_quantity": rc.get("purchase_quantity"), "sale_quantity": rc.get("sale_quantity"),
            "deals_in": rc.get("deals_in", ""), "whom_to_sell": rc.get("whom_to_sell", ""),
            "call_mode": rc.get("call_mode", ""), "call_type": rc.get("call_type", ""),
            "next_followup_date": next_fu.strftime("%Y-%m-%d") if next_fu else "",
            "outcome": outcome, "assigned_to": d.assigned_to or "",
        }
        row_json = esc(json.dumps(row_data))

        status_cell = (
            f'<span class="badge bg-{_OC_COLORS.get(outcome, "secondary")} bg-opacity-75">'
            f'{esc(OUTCOME_LABELS.get(outcome, (outcome or "").replace("_", " ").title()))}</span>'
        ) if outcome else '<span class="text-muted small">—</span>'

        if next_fu:
            fu_cls = "bg-danger" if next_fu.date() <= today else "bg-info text-dark"
            fu_cell = f'<span class="badge {fu_cls}">{next_fu.strftime("%d-%m-%Y")}</span>'
        else:
            fu_cell = '<span class="text-muted">—</span>'

        assigned_cell = (esc(assignee_name_map.get(d.assigned_to, d.assigned_to))
                         if d.assigned_to else '<span class="text-muted">Unassigned</span>')

        actions = (
            f'<div class="btn-group btn-group-sm">'
            f'<button type="button" class="btn btn-outline-primary" title="View" '
            f'onclick=\'openDealerViewModal({row_json})\'><i class="bi bi-eye"></i></button>'
            f'<a href="/dealers/{d.id}/call" class="btn btn-outline-success" title="Log Call">'
            f'<i class="bi bi-telephone-plus"></i></a>'
            f'<button type="button" class="btn btn-outline-secondary" title="Edit" '
            f'onclick=\'openDealerEditModal({row_json})\'><i class="bi bi-pencil"></i></button>'
            f'<button type="button" class="btn btn-outline-info" title="Quotation" '
            f'onclick=\'openQuotationModal({row_json})\'><i class="bi bi-file-earmark-text"></i></button>'
        )
        if can_manage_dealers:
            actions += (
                f'<form action="/dealers/{d.id}/delete" method="post" class="d-inline" '
                f'data-dealer-name="{esc(d.business_name or "")}" onsubmit="return confirmDealerDelete(this)">'
                f'<input type="hidden" name="csrf_token" value="{esc(request.cookies.get("csrf_token", ""))}">'
                f'<button type="submit" class="btn btn-outline-danger" title="Delete">'
                f'<i class="bi bi-trash"></i></button></form>'
            )
        actions += "</div>"

        row = []
        if can_manage_dealers:
            row.append(f'<input type="checkbox" class="form-check-input dealerChk" value="{d.id}">')
        # Dealer ID column hidden by request (the code still shows in the View
        # popup); Status sits after Sub Category.
        row.extend([
            f'<a href="/dealers/{d.id}" class="fw-semibold text-decoration-none">{esc(d.business_name or "")}</a>',
            f'<span class="badge bg-secondary">{esc((d.dealer_type or "").title())}</span>',
            f'<span class="small">{esc(d.dealer_sub_category or "—")}</span>',
            status_cell,
            f'<span class="small">{esc(d.contact_person or "—")}</span>',
            f'<span class="small">{esc(d.phone or "—")}</span>',
            f'<span class="small">{esc(d.email or "—")}</span>',
            f'<span class="small">{esc(_truncate(d.address) or "—")}</span>',
            f'<span class="small">{esc(d.city or "—")}</span>',
            f'<span class="small">{esc(d.state or "—")}</span>',
            f'<span class="small">{call_date.strftime("%d-%m-%Y") if call_date else "—"}</span>',
            f'<span class="small">{rc.get("qty") if rc.get("qty") is not None else "—"}</span>',
            f'<span class="small">{esc(_truncate(rc.get("items_text", "")) or "—")}</span>',
            f'<span class="small">{esc(_truncate(rc.get("calling_remark", "")) or "—")}</span>',
            f'<span class="small">{esc(_truncate(rc.get("notes", "")) or "—")}</span>',
            f'<span class="small">{rc.get("purchase_quantity") if rc.get("purchase_quantity") is not None else "—"}</span>',
            f'<span class="small">{rc.get("sale_quantity") if rc.get("sale_quantity") is not None else "—"}</span>',
            f'<span class="small">{esc(rc.get("deals_in") or "—")}</span>',
            f'<span class="small">{esc(rc.get("whom_to_sell") or "—")}</span>',
            f'<span class="small">{fu_cell}</span>',
            f'<span class="small">{assigned_cell}</span>',
            actions,
        ])
        data.append(row)

    return JSONResponse({
        "draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data,
    })


@router.get("/ids")
async def dealers_ids(
    q: str = Query(default=""), status: str = Query(default=""),
    assigned: str = Query(default=""), city: str = Query(default=""),
    calling_date_from: str = Query(default=""), calling_date_to: str = Query(default=""),
    filter_qty: str = Query(default=""), filter_purchase_qty: str = Query(default=""),
    filter_sale_qty: str = Query(default=""), filter_whom_to_sell: str = Query(default=""),
    filter_deals_in: str = Query(default=""),
    followup_from: str = Query(default=""), followup_to: str = Query(default=""),
    dealer_type: str = Query(default=""), sub_category: str = Query(default=""),
    search_value: str = Query(default="", alias="search[value]"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Every dealer id matching the Dealers table's current filters + search —
    exactly the set /dealers/data pages through. Backs the header "select all"
    checkbox, which selects all matching rows, not just the page on screen."""
    base_query = _dealer_base_query(
        current_user, q, status, assigned, city, calling_date_from, calling_date_to,
        filter_qty, filter_purchase_qty, filter_sale_qty, filter_whom_to_sell,
        filter_deals_in, followup_from, followup_to, dealer_type, sub_category,
    )
    base_query = _dealer_text_search(base_query, search_value)
    sub = base_query.subquery()
    ids = (await db.execute(select(sub.c.id))).scalars().all()
    return JSONResponse({"ids": [str(i) for i in ids]})


_PHONE_LIKE = None


def _csv_cell(value) -> str:
    """Text for one exported cell. A value Excel would read as a formula
    (=, @, or +/- followed by more than a phone number) gets a leading quote,
    so an imported business name can never run as a formula on open."""
    import re
    global _PHONE_LIKE
    if _PHONE_LIKE is None:
        _PHONE_LIKE = re.compile(r"^[+\-]?[\d\s\-()+,/]*$")
    s = "" if value is None else str(value)
    if s[:1] in ("=", "@", "\t", "\r") or (s[:1] in ("+", "-") and not _PHONE_LIKE.match(s)):
        return "'" + s
    return s


DEALER_EXPORT_HEADER = [
    "Dealer Code", "Business Name", "Type", "Sub Category", "Status",
    "Contact Person", "Phone", "Email", "Address", "City", "State", "Pincode",
    "Website", "Google CID", "GSTIN", "Last Call Date", "Qty", "Items Discussed",
    "Calling Remark", "Notes", "Purchase Qty", "Sale Qty", "Deals In",
    "Whom to Sell", "Next Follow-up", "Assigned To", "Source", "Added On",
]


async def dealer_export_response(db: AsyncSession, dealer_ids: list, filename_prefix: str = "dealers"):
    """CSV download of the given dealers (Dealer Management and Assign Dealer
    Leads share it). Newest added first, like both tables; the Assigned To
    column carries the user's display name. Ids are looked up in chunks to stay
    under Postgres' bound-parameter limit."""
    CHUNK = 5000
    dealers = []
    for i in range(0, len(dealer_ids), CHUNK):
        dealers.extend((await db.execute(
            select(Dealer).where(Dealer.id.in_(dealer_ids[i:i + CHUNK]), Dealer.trashed_at.is_(None))
        )).scalars().all())
    dealers.sort(key=lambda d: d.dealer_code or "", reverse=True)
    dealers.sort(key=lambda d: (d.created_at is not None, d.created_at or datetime.min), reverse=True)

    recent_call_map = {}
    for i in range(0, len(dealers), CHUNK):
        recent_call_map.update(await _recent_call_map(db, [d.id for d in dealers[i:i + CHUNK]]))
    names = await _assignee_name_map(db, dealers)

    def fmt(dt, f):
        return dt.strftime(f) if dt else ""

    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel opens ₹, Hindi names etc. as UTF-8
    w = csv.writer(buf)
    w.writerow(DEALER_EXPORT_HEADER)
    for d in dealers:
        rc = recent_call_map.get(str(d.id), {})
        outcome = rc.get("outcome", "")
        w.writerow([_csv_cell(v) for v in (
            d.dealer_code, d.business_name, (d.dealer_type or "").title(), d.dealer_sub_category,
            OUTCOME_LABELS.get(outcome, (outcome or "").replace("_", " ").title()),
            d.contact_person, d.phone, d.email, d.address, d.city, d.state, d.pincode,
            d.website, d.google_cid, d.gstin,
            fmt(rc.get("call_date"), "%d-%m-%Y %H:%M"), rc.get("qty"), rc.get("items_text", ""),
            rc.get("calling_remark", ""), rc.get("notes", ""),
            rc.get("purchase_quantity"), rc.get("sale_quantity"),
            rc.get("deals_in", ""), rc.get("whom_to_sell", ""),
            fmt(rc.get("next_followup_date"), "%d-%m-%Y"),
            (names.get(d.assigned_to) or d.assigned_to) if d.assigned_to else "Unassigned",
            d.source, fmt(d.created_at, "%d-%m-%Y %H:%M"),
        )])
    filename = f"{filename_prefix}_{app_now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _split_ids(raw: str) -> list:
    """Comma-joined dealer ids from an export form (one field, so a large
    selection never trips Starlette's 1000-form-field limit). Non-UUIDs are
    dropped."""
    import uuid
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        try:
            out.append(uuid.UUID(part))
        except ValueError:
            continue
    return out


@router.post("/export")
async def export_dealers(
    request: Request,
    dealer_ids: str = Form(default=""),
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Export button on Dealer Management: the page sends the ids of the
    selected rows, or of every row matching its filters + search (from
    /dealers/ids). Re-checked against the viewer's own scope, so a Telecaller
    can only ever export their own dealers."""
    ids = _split_ids(dealer_ids)
    scope = _dealer_base_query(current_user).subquery()
    allowed = set((await db.execute(select(scope.c.id))).scalars().all())
    ids = [i for i in ids if i in allowed]
    await audit(
        db, user=current_user, action="DEALERS_EXPORTED", table_name="dealers",
        record_id=f"export:{len(ids)}", new_value={"count": len(ids), "page": "dealer_management"}, request=request,
    )
    await db.commit()
    return await dealer_export_response(db, ids, "dealers")


@router.post("/bulk-delete")
async def bulk_delete_dealers(
    request: Request,
    dealer_ids: list[str] = Form(default=[]),
    return_to: str = Form(default="/dealers"),
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer_manager),
):
    """Permanently delete the selected dealers and their call log / telecalling
    history. Dealers holding financial records are archived instead and reported,
    so the count shown is what actually happened rather than what was clicked.
    Used by Dealer Management and Assign Dealer Leads (return_to = the page to
    land back on; only a local path is accepted)."""
    back = return_to if (return_to or "").startswith("/") and not return_to.startswith("//") else "/dealers"
    back = back.split("?")[0]
    ids = [i for i in (dealer_ids or []) if i]
    if not ids:
        return RedirectResponse(url=f"{back}?error=No+dealers+selected", status_code=302)

    dealers = (await db.execute(select(Dealer).where(Dealer.id.in_(ids)))).scalars().all()
    if not dealers:
        return RedirectResponse(url=f"{back}?error=No+dealers+found", status_code=302)
    found_ids = [str(d.id) for d in dealers]

    deletable, blocked = await _partition_deletable(db, found_ids)

    # Dealers we must keep are at least archived, so they leave the working list.
    now = app_now()
    for d in dealers:
        if str(d.id) in blocked and d.trashed_at is None:
            d.trashed_at = now
            d.trashed_by = current_user.username

    await audit(
        db, user=current_user, action="DEALER_BULK_DELETED",
        table_name="dealers", record_id=f"bulk:{len(deletable)}",
        old_value={"deleted_dealers": [
            {"id": str(d.id), "business_name": d.business_name, "phone": d.phone}
            for d in dealers if str(d.id) in deletable
        ][:500]},
        new_value={"deleted": len(deletable), "retained_for_financials": blocked,
                   "by": current_user.username},
        request=request,
    )
    counts = await _hard_delete_dealers(db, deletable)
    await db.commit()

    calls = counts.get("dealer_calls", 0)
    # The pages send large selections in batches (the server refuses a request
    # with 1000+ form fields), so they ask for a JSON reply and total it up.
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"success": True, "deleted": counts.get("dealers", 0),
                             "calls_removed": calls, "kept": len(blocked)})

    import urllib.parse
    parts = [f"{counts.get('dealers', 0)} dealer(s) deleted permanently"]
    if calls:
        parts.append(f"{calls} call log(s) removed")
    msg = urllib.parse.quote(" — ".join(parts))
    url = f"{back}?success={msg}"
    if blocked:
        url += "&warning=" + urllib.parse.quote(
            f"{len(blocked)} dealer(s) kept: they hold financial records "
            f"(orders/credit notes/quotations/receipts) that must be retained.")
    return RedirectResponse(url=url, status_code=302)


# ── Hard delete ──────────────────────────────────────────────────────────────
# Delete used to only set trashed_at. The row stayed, kept its phone and business
# name, and so kept blocking any re-upload of the same dealer — the user deleted
# data they could no longer see and had no way to replace it.
#
# Tables carrying a dealer's operational history. These are removed with the
# dealer; a call log for a dealer that no longer exists has no meaning, and the
# FK is NOT NULL on most of them so the delete would fail otherwise.
# Ordered children-first: FKs must be cleared before the parent row goes.
_DEALER_CHILD_TABLES = [
    "dealer_calls",
    "dealer_assignments",
    "telecalling_records",
    "telecalling_assignments",
    "whatsapp_messages",
    "partner_bookings",
    "partner_login_logs",
    "partner_listing_views",
    "lot_dealer_visibility",
    "lot_booking_requests",
    "partner_bids",
]

# Financial and statutory records. A dealer holding any of these is NOT deleted —
# orders, credit notes, receipts and quotations are books of account, and Indian
# retention rules (and this project's own 7-year transaction-log standard) require
# keeping them. Such dealers are trashed instead and reported back, so the caller
# learns why rather than silently believing the delete happened.
_DEALER_FINANCIAL_TABLES = [
    ("dealer_orders", "orders"),
    ("dealer_credit_notes", "credit notes"),
    ("dealer_quotations", "quotations"),
    ("customer_receipts", "receipts"),
]


async def _partition_deletable(db: AsyncSession, ids: list[str]) -> tuple[list[str], dict]:
    """Split dealer ids into (safe to hard-delete, {id: reason}) — one query per
    financial table for the whole batch, not per dealer."""
    blocked: dict = {}
    for table, label in _DEALER_FINANCIAL_TABLES:
        rows = (await db.execute(
            sa_text(f"SELECT DISTINCT dealer_id FROM {table} WHERE dealer_id = ANY(:ids)"),
            {"ids": ids},
        )).scalars().all()
        for did in rows:
            blocked.setdefault(str(did), []).append(label)
    deletable = [i for i in ids if str(i) not in blocked]
    return deletable, {k: ", ".join(v) for k, v in blocked.items()}


async def _hard_delete_dealers(db: AsyncSession, ids: list[str]) -> dict:
    """Remove dealers and their operational history. Returns per-table counts.

    Set-based: a handful of statements for the whole batch rather than one per
    dealer. The database is in a different region from the app, so a per-row loop
    here would take minutes on a large selection.
    """
    counts: dict = {}
    if not ids:
        return counts
    # partner_bid_documents / partner_bid_payments carry a NOT NULL FK to
    # partner_bids.id (no cascade) — they reference a bid, not a dealer, so
    # they're invisible to the generic dealer_id loop below. Deleting
    # partner_bids first without clearing these would fail with a foreign-key
    # violation (this was the cause of the 500 on the partner Delete button).
    for grandchild in ("partner_bid_documents", "partner_bid_payments"):
        try:
            res = await db.execute(sa_text(
                f"DELETE FROM {grandchild} WHERE bid_id IN "
                f"(SELECT id FROM partner_bids WHERE dealer_id = ANY(:ids))"
            ), {"ids": ids})
            if res.rowcount:
                counts[grandchild] = res.rowcount
        except Exception:
            continue
    for table in _DEALER_CHILD_TABLES:
        try:
            res = await db.execute(
                sa_text(f"DELETE FROM {table} WHERE dealer_id = ANY(:ids)"), {"ids": ids}
            )
            if res.rowcount:
                counts[table] = res.rowcount
        except Exception:
            # A table that doesn't exist in this deployment must not abort the
            # whole delete — skip it and carry on with the rest.
            continue
    res = await db.execute(sa_text("DELETE FROM dealers WHERE id = ANY(:ids)"), {"ids": ids})
    counts["dealers"] = res.rowcount
    return counts


@router.post("/{dealer_id}/delete")
async def delete_dealer(
    request: Request,
    dealer_id: str,
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_dealer_manager),
):
    """Permanently delete a dealer and its call log / telecalling history.

    A dealer carrying financial records (orders, credit notes, quotations,
    receipts) is trashed instead of deleted — those are books of account and must
    be retained. The response says which happened, so "deleted" never means
    "still there, invisible"."""
    dealer = (await db.execute(
        select(Dealer).where(Dealer.id == dealer_id)
    )).scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Dealer+not+found", status_code=302)

    name = dealer.business_name or str(dealer.id)
    deletable, blocked = await _partition_deletable(db, [str(dealer.id)])

    if not deletable:
        # Keep the row, but make sure it is trashed and say why it survived.
        if dealer.trashed_at is None:
            dealer.trashed_at = app_now()
            dealer.trashed_by = current_user.username
        reason = blocked.get(str(dealer.id), "financial records")
        await audit(
            db, user=current_user, action="DEALER_TRASHED_HAS_FINANCIALS",
            table_name="dealers", record_id=str(dealer.id),
            new_value={"business_name": name, "retained_because": reason},
            request=request,
        )
        await db.commit()
        import urllib.parse
        msg = urllib.parse.quote(
            f"{name} has {reason} and was archived instead of deleted — "
            f"financial records must be retained.")
        return RedirectResponse(url=f"/dealers?warning={msg}", status_code=302)

    # Audit BEFORE the delete — afterwards there is no row left to describe.
    await audit(
        db, user=current_user, action="DEALER_DELETED",
        table_name="dealers", record_id=str(dealer.id),
        old_value={"business_name": name, "phone": dealer.phone,
                   "dealer_code": dealer.dealer_code},
        new_value={"deleted": True, "by": current_user.username},
        request=request,
    )
    counts = await _hard_delete_dealers(db, deletable)
    await db.commit()
    calls = counts.get("dealer_calls", 0)
    import urllib.parse
    msg = urllib.parse.quote(
        f"{name} deleted permanently" + (f" with {calls} call log(s)" if calls else ""))
    return RedirectResponse(url=f"/dealers?success={msg}", status_code=302)


@router.get("/followups-due", response_class=HTMLResponse)
async def followups_due(
    request: Request,
    today_only: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """All dealers whose LATEST call log has a next_followup_date set —
    not just calls due today. Ranked-latest-call subquery (rn == 1 per
    dealer), same pattern used for the followup_count badge on the list page.
    Pass ?today_only=1 to restrict to follow-ups scheduled for today."""
    today = app_now().date()

    rn_col = func.row_number().over(
        partition_by=DealerCall.dealer_id,
        order_by=DealerCall.call_date.desc()
    ).label("rn")
    ranked = select(
        DealerCall.id,
        DealerCall.dealer_id,
        DealerCall.next_followup_date,
        rn_col,
    ).where(DealerCall.next_followup_date.isnot(None)).subquery()

    stmt = (
        select(DealerCall, Dealer.business_name, Dealer.id.label("dealer_id_col"), Dealer.phone)
        .join(ranked, DealerCall.id == ranked.c.id)
        .join(Dealer, DealerCall.dealer_id == Dealer.id)
        .where(ranked.c.rn == 1)
        .order_by(DealerCall.next_followup_date)
    )
    if today_only:
        stmt = stmt.where(func.date(DealerCall.next_followup_date) == today)
    if current_user.role not in (UserRole.admin, UserRole.sales):
        stmt = stmt.where(Dealer.assigned_to == current_user.username)
    rows = (await db.execute(stmt)).all()
    return templates.TemplateResponse("dealers/followups.html", {
        "request": request, "current_user": current_user, "rows": rows, "today": today,
        "today_only": today_only,
    })


@router.get("/overdue", response_class=HTMLResponse)
async def overdue_orders(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):

    now = app_now()  # naive UTC — must match DB column (DateTime, no tz)
    rows = (await db.execute(
        select(DealerOrder, Dealer)
        .join(Dealer, DealerOrder.dealer_id == Dealer.id)
        .where(
            DealerOrder.due_amount > 0,
            DealerOrder.payment_due_date.isnot(None),
            DealerOrder.payment_due_date < now,
        )
        .order_by(DealerOrder.payment_due_date)
    )).all()

    overdue = []
    for order, dealer in rows:
        days_overdue = (now - order.payment_due_date).days
        overdue.append({
            "order": order,
            "dealer": dealer,
            "days_overdue": days_overdue,
        })

    return templates.TemplateResponse("dealers/overdue.html", {
        "request": request,
        "current_user": current_user,
        "overdue": overdue,
        "total_due": sum(float(item["order"].due_amount) for item in overdue),
    })


@router.get("/ageing", response_class=HTMLResponse)
async def dealer_ageing(
    request: Request,
    export: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Dealer ageing analysis — buckets outstanding amounts by overdue days."""

    rows = (await db.execute(
        select(DealerOrder, Dealer)
        .join(Dealer, DealerOrder.dealer_id == Dealer.id)
        .where(
            DealerOrder.due_amount > 0,
            DealerOrder.status.in_(OUTSTANDING_STATUSES),
        )
        .order_by(Dealer.business_name)
    )).all()

    now = app_now()  # naive UTC — matches DB column (DateTime, no tz)
    dealer_map: dict = {}
    for order, dealer in rows:
        key = str(dealer.id)
        if key not in dealer_map:
            dealer_map[key] = {
                "dealer": dealer,
                "current": 0.0,
                "d30": 0.0,
                "d60": 0.0,
                "d90": 0.0,
                "d90plus": 0.0,
                "total": 0.0,
            }
        amt = float(order.due_amount or 0)
        dealer_map[key]["total"] += amt
        if order.payment_due_date is None or order.payment_due_date >= now:
            dealer_map[key]["current"] += amt
        else:
            days = (now - order.payment_due_date).days
            if days <= 30:
                dealer_map[key]["d30"] += amt
            elif days <= 60:
                dealer_map[key]["d60"] += amt
            elif days <= 90:
                dealer_map[key]["d90"] += amt
            else:
                dealer_map[key]["d90plus"] += amt

    ageing_rows = list(dealer_map.values())
    totals = {
        k: sum(r[k] for r in ageing_rows)
        for k in ("current", "d30", "d60", "d90", "d90plus", "total")
    }

    if export == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Dealer", "Current", "1-30 Days", "31-60 Days", "61-90 Days", "90+ Days", "Total"])
        for r in ageing_rows:
            writer.writerow([
                r["dealer"].business_name,
                f"{r['current']:.2f}",
                f"{r['d30']:.2f}",
                f"{r['d60']:.2f}",
                f"{r['d90']:.2f}",
                f"{r['d90plus']:.2f}",
                f"{r['total']:.2f}",
            ])
        writer.writerow([
            "TOTAL",
            f"{totals['current']:.2f}",
            f"{totals['d30']:.2f}",
            f"{totals['d60']:.2f}",
            f"{totals['d90']:.2f}",
            f"{totals['d90plus']:.2f}",
            f"{totals['total']:.2f}",
        ])
        filename = f"dealer-ageing-{now.strftime('%Y%m%d')}.csv"
        return StreamingResponse(
            iter([output.getvalue().encode("utf-8-sig")]),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return templates.TemplateResponse("dealers/ageing.html", {
        "request": request,
        "current_user": current_user,
        "ageing_rows": ageing_rows,
        "totals": totals,
        "as_of": now,
    })


@router.get("/bulk-upload-template")
async def dealers_bulk_upload_template():
    """Return a sample CSV template for dealer bulk upload."""
    header = ("business_name,contact_person,phone,email,city,state,pincode,dealer_type,"
              "dealer_sub_category,gst_number,address,website,google_cid,assigned_to\n")
    sample = (
        "Example Traders,Rajesh Kumar,9876543210,rajesh@example.com,Delhi,Delhi,110005,wholesale,"
        "Computer Store,07ABCDE1234F1Z5,123 Main Street Karol Bagh,https://example.com,,sales_user\n"
        "ABC Electronics,Priya Sharma,9123456789,priya@abc.com,Mumbai,Maharashtra,400053,retail,"
        ",,456 MG Road Andheri,,,\n"
    )
    content = header + sample
    return StreamingResponse(
        iter([content.encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=dealers_bulk_upload_template.csv"},
    )


@router.get("/bulk-upload", response_class=HTMLResponse)
async def dealers_bulk_upload_form(
    request: Request,
    current_user: User = Depends(require_sales),
):
    return templates.TemplateResponse("dealers/bulk_upload.html", {
        "request": request, "current_user": current_user,
        "results": None, "error": None, "detected_cols": [],
    })


@router.post("/bulk-upload", response_class=HTMLResponse)
async def dealers_bulk_upload_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    from fastapi import UploadFile, File
    form = await request.form()
    upload = form.get("file")
    if not upload or not hasattr(upload, "read"):
        return templates.TemplateResponse("dealers/bulk_upload.html", {
            "request": request, "current_user": current_user,
            "results": None, "error": "No file uploaded.",
        })

    raw = await upload.read()
    filename = (getattr(upload, "filename", "") or "").lower()

    # ── Column-name normaliser ─────────────────────────────────────────────────
    def _norm(s) -> str:
        """Safely lowercase + strip + collapse spaces/hyphens → underscores."""
        if not s or not isinstance(s, str):
            return ""
        return s.lower().strip().replace(" ", "_").replace("-", "_")

    _COL_ALIASES = {
        "businessname":   "business_name", "company_name":  "business_name",
        "company":        "business_name", "name":          "business_name",
        "dealer_name":    "business_name", "dealername":    "business_name",
        "firm_name":      "business_name", "firmname":      "business_name",
        "shop_name":      "business_name", "shopname":      "business_name",
        "contact":        "contact_person","contactperson": "contact_person",
        "contact_name":   "contact_person","person":        "contact_person",
        "mobile":         "phone",         "phone_number":  "phone",
        "mobile_number":  "phone",         "contact_no":    "phone",
        "gst":            "gst_number",    "gstin":         "gst_number",
        "gst_no":         "gst_number",    "gstno":         "gst_number",
        "gst_in":         "gst_number",
        "type":           "dealer_type",   "dealertype":    "dealer_type",
        # "category" is resolved per row: a valid Dealer Type value sets Type
        # (older templates used it that way), anything else is a Sub Category
        # (a Google Maps export's "Category": Optician, Hospital, ...).
        "sub_category":   "dealer_sub_category", "subcategory": "dealer_sub_category",
        "dealer_subcategory": "dealer_sub_category",
        "email_1":        "email",         "email1":        "email",
        "pin":            "pincode",       "pin_code":      "pincode",
        "zip":            "pincode",       "postal_code":   "pincode",
        "web":            "website",       "website_url":   "website",
        "cid":            "google_cid",    "google_maps_cid": "google_cid",
        "assigned":       "assigned_to",   "assignedto":    "assigned_to",
        "sales_person":   "assigned_to",   "salesperson":   "assigned_to",
    }

    def _map_headers(raw_headers: list) -> list:
        """Normalise a list of raw header strings to canonical column names."""
        mapped = []
        for h in raw_headers:
            n = _norm(h)
            mapped.append(_COL_ALIASES.get(n, n) if n else "")
        return mapped

    def _normalise_row(raw_row: dict) -> dict:
        out = {}
        for k, v in raw_row.items():
            n = _norm(k)
            if not n:           # skip None / empty keys (DictReader restkey)
                continue
            canonical = _COL_ALIASES.get(n, n)
            out[canonical] = str(v).strip() if v is not None else ""
        return out

    # ── Parse CSV or XLSX ──────────────────────────────────────────────────────
    rows_data: list  = []
    detected_cols: list = []
    parse_error:  str  = ""

    try:
        if filename.endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
            ws = wb.active
            headers = None
            for r in ws.iter_rows(values_only=True):
                if all(c is None for c in r):
                    continue
                if headers is None:
                    headers = _map_headers([str(c) if c is not None else "" for c in r])
                    detected_cols = [h for h in headers if h]
                    continue
                row_dict = dict(zip(headers, [str(v).strip() if v is not None else "" for v in r]))
                rows_data.append(row_dict)
        else:
            # CSV — auto-detect encoding then delimiter
            text_content = decode_csv_bytes(raw)
            text_content = text_content.replace('\r\n', '\n').replace('\r', '\n').strip()

            # Auto-detect delimiter (comma, semicolon, tab, pipe)
            sample = text_content[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
            except csv.Error:
                dialect = csv.excel          # fallback: standard comma CSV

            reader = csv.DictReader(io.StringIO(text_content), dialect=dialect)
            # Force reading fieldnames so we can remap them before iterating
            raw_fields = reader.fieldnames or []
            reader.fieldnames = _map_headers(raw_fields)
            detected_cols = [h for h in reader.fieldnames if h]

            for row in reader:
                rows_data.append(_normalise_row(row))

    except Exception as exc:
        parse_error = f"Could not parse file: {exc}"

    if parse_error:
        return templates.TemplateResponse("dealers/bulk_upload.html", {
            "request": request, "current_user": current_user,
            "results": None, "error": parse_error,
        })

    if not rows_data:
        return templates.TemplateResponse("dealers/bulk_upload.html", {
            "request": request, "current_user": current_user,
            "results": None,
            "error": (
                "No data rows found in the file. "
                f"Columns detected: {detected_cols or 'none'}. "
                "Make sure the file has a header row and at least one data row."
            ),
        })

    # Warn early if business_name column was not detected at all
    col_warning = ""
    if "business_name" not in detected_cols:
        col_warning = (
            f"Column 'business_name' not found. "
            f"Columns detected in your file: {detected_cols}. "
            "Rename your header to 'business_name' and re-upload."
        )

    # ── Import rows ────────────────────────────────────────────────────────────
    # Codes come from MAX(existing code), not count(*). See utils/dealer_code:
    # two code formats share one sequence, so the numbering runs ahead of the row
    # count and count(*)+1 eventually returns a code that already exists — a
    # UNIQUE violation that fails the whole import.
    alloc_code = await dealer_code_allocator(db)

    # Duplicate checks consider LIVE dealers only. Deleting a dealer here is a
    # soft delete (trashed_at), and without this filter a trashed row kept
    # blocking its own re-upload forever: the user deletes the bad data, tries to
    # upload the corrected file, and every row is rejected as "already exists"
    # against a dealer they can no longer see anywhere in the UI. The only escape
    # was to destroy the rows outright, taking their call history with them.
    phones_result = await db.execute(
        select(Dealer.phone).where(Dealer.phone.isnot(None), Dealer.trashed_at.is_(None))
    )
    existing_phones = {r for r in phones_result.scalars().all()}

    # Existing business names — matched case-insensitively so "ABC Traders" and
    # "abc traders" are treated as the same dealer, not two separate ones.
    names_result = await db.execute(
        select(Dealer.business_name).where(
            Dealer.business_name.isnot(None), Dealer.trashed_at.is_(None)
        )
    )
    existing_names_lower = {r.strip().lower() for r in names_result.scalars().all()}

    added = []
    skipped = []
    errors = []

    seq = 0
    valid_types = {"retail", "wholesale", "online", "corporate"}
    state_options = await master_values(db, "dealer_state")

    # Google CID is a true identity (one place on Google Maps), unlike name or
    # phone, so it is the one duplicate check kept: uploading the same export
    # twice must not create every dealer again. Live dealers only, as above.
    existing_cids = set((await db.execute(
        select(Dealer.google_cid).where(Dealer.google_cid.isnot(None), Dealer.trashed_at.is_(None))
    )).scalars().all())
    sub_categories_seen: list = []

    for i, row in enumerate(rows_data, start=2):  # row 1 = header
        business_name = row.get("business_name", "").strip()
        if not business_name:
            skipped.append({"row": i, "reason": "business_name is empty"})
            continue
        # Duplicate checks removed by request: the same business name or phone may
        # legitimately appear many times (multiple branches, shared landlines,
        # re-canvassing the same shop). Rejecting them silently dropped real rows
        # and the operator could not tell which. Every row is now imported.

        google_cid = parse_google_cid(row.get("google_cid", ""))
        if google_cid and google_cid in existing_cids:
            skipped.append({"row": i, "business_name": business_name,
                            "reason": f"Google CID {google_cid} already uploaded"})
            continue

        # Phone cell may hold multiple numbers (comma/slash/semicolon/pipe-separated,
        # or just a long run of digits) — accepted and stored as-is, no format rejection.
        phone_raw = row.get("phone", "").strip()
        phone = phone_raw or None

        category_raw = row.get("category", "").strip()
        dealer_type = (row.get("dealer_type", "").strip().lower()
                       or (category_raw.lower() if category_raw.lower() in valid_types else "")
                       or "retail")
        if dealer_type not in valid_types:
            dealer_type = "retail"
        sub_category = row.get("dealer_sub_category", "").strip()
        if not sub_category and category_raw and category_raw.lower() not in valid_types:
            sub_category = category_raw
        sub_category = sub_category[:150] or None
        if sub_category:
            sub_categories_seen.append(sub_category)

        pincode = (row.get("pincode", "").strip() or None)
        if pincode:
            pincode = pincode[:10]
        state = normalize_state(row.get("state", ""), pincode, state_options)

        # Google Maps exports name the search that found the row; keep it as
        # the dealer's Source so imported leads can be traced back.
        keyword = row.get("keyword", "").strip()
        location = row.get("location", "").strip()
        source = None
        if keyword or google_cid:
            source = "Google Maps" + (f": {keyword}" if keyword else "") + (f" ({location})" if location else "")

        dealer_code = alloc_code()
        seq += 1

        try:
            dealer = Dealer(
                dealer_code=dealer_code,
                business_name=business_name[:200],
                contact_person=row.get("contact_person", "").strip() or None,
                phone=phone,
                email=row.get("email", "").strip() or None,
                city=(row.get("city", "").strip()[:100] or None),
                state=state,
                pincode=pincode,
                dealer_type=dealer_type,
                dealer_sub_category=sub_category,
                gstin=row.get("gst_number", "").strip() or None,
                address=row.get("address", "").strip() or None,
                website=(row.get("website", "").strip()[:1000] or None),
                google_cid=google_cid,
                source=source,
                # Blank assigned_to = Unassigned, so uploaded leads land in Assign
                # Dealer Leads' Unassigned pool. A telecaller's upload still
                # defaults to themselves: their Dealer Management list only shows
                # dealers assigned to them, so unassigned rows would vanish.
                assigned_to=(row.get("assigned_to", "").strip()
                             or (current_user.username if current_user.role == UserRole.telecaller else None)),
                status="active",
                created_by=current_user.username,
                added_by=current_user.username,
            )
            db.add(dealer)
            if phone:
                existing_phones.add(phone)
            if google_cid:
                existing_cids.add(google_cid)
            existing_names_lower.add(business_name.lower())
            added.append({"row": i, "business_name": business_name, "dealer_code": dealer_code})
        except Exception as exc:
            errors.append({"row": i, "business_name": business_name, "reason": str(exc)})

    # New Sub Category values from the file join the Master Dropdown, so the
    # Add/Edit Dealer dropdown can show what these dealers were imported with.
    new_sub_categories = []
    if added and sub_categories_seen:
        new_sub_categories = await add_missing_master_values(db, "dealer_sub_category", sub_categories_seen)

    if added:
        await audit(
            db, user=current_user, action="DEALER_BULK_UPLOADED",
            table_name="dealers", record_id=f"bulk:{len(added)}",
            new_value={"file": getattr(upload, "filename", "") or "", "added": len(added),
                       "skipped": len(skipped), "errors": len(errors),
                       "new_sub_categories": new_sub_categories[:200]},
            request=request,
        )
        await db.commit()
        if new_sub_categories:
            await refresh_master_cache(db)

    results = {"added": added, "skipped": skipped, "errors": errors,
               "new_sub_categories": new_sub_categories}
    return templates.TemplateResponse("dealers/bulk_upload.html", {
        "request": request, "current_user": current_user,
        "results": results,
        "error": col_warning or None,
        "detected_cols": detected_cols,
    })


@router.get("/calls/bulk-upload-template")
async def dealer_calls_bulk_upload_template(
    current_user: User = Depends(require_sales),
):
    """Return a sample CSV template for dealer call-record bulk upload.

    Guarded with the same require_sales as the upload endpoint it feeds. The
    payload is only fictional sample rows, but an unauthenticated endpoint on a
    public host still advertises the module's existence and its exact field
    names, and there is no reason for it to answer anyone who cannot use it.
    """
    header = (
        "dealer_phone,business_name,call_date,call_type,call_mode,duration_mins,call_outcome,"
        "items_discussed,quote_given,next_followup_date,notes,calling_remark,category,"
        "product_model,configuration,qty,asking_price,deal_status,assigned_to\n"
    )
    sample = (
        "9876543210,Example Traders,2026-07-15,outbound,phone,12,interested,"
        "i5 8th Gen laptops,45000,2026-07-22,Wants 20 units,Price sensitive,Laptop,"
        "ThinkPad T480,i5/8GB/256GB,20,44000,negotiation,sales_user\n"
        ",ABC Electronics,2026-07-16,inbound,whatsapp,,callback,"
        "Desktops,,,Asked for quote,,Desktop,,,,,open,\n"
    )
    content = header + sample
    return StreamingResponse(
        iter([content.encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=dealer_calls_bulk_upload_template.csv"},
    )


@router.post("/calls/bulk-upload", response_class=HTMLResponse)
async def dealer_calls_bulk_upload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
    _csrf: None = Depends(verify_csrf),
):
    """Bulk-add DealerCall rows from CSV via the Dealer Management page.

    Each row is resolved to an existing dealer by phone (preferred) or by
    business_name (case-insensitive fallback). Rows that cannot be resolved or
    fail validation are skipped and reported — one bad row never aborts the
    whole upload. Dealer lookup is done with two set-based IN queries rather
    than per-row round trips (the DB lives in a different region)."""
    form = await request.form()
    upload = form.get("file")
    if not upload or not hasattr(upload, "read"):
        return RedirectResponse(url="/dealers?error=No+file+uploaded", status_code=302)

    raw = await upload.read()
    text = decode_csv_bytes(raw)

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # ── Header-name matching (case-insensitive, order-independent) ──────────
    def _norm(s) -> str:
        if not s or not isinstance(s, str):
            return ""
        return s.lower().strip().replace(" ", "_").replace("-", "_")

    _CALL_ALIASES = {
        "phone":          "dealer_phone", "mobile":        "dealer_phone",
        "mobile_number":  "dealer_phone", "contact_no":    "dealer_phone",
        "dealer_mobile":  "dealer_phone", "phone_number":  "dealer_phone",
        "dealer_name":    "business_name", "company":      "business_name",
        "company_name":   "business_name", "firm_name":    "business_name",
        "date":           "call_date",     "called_on":    "call_date",
        "mode":           "call_mode",     "type":         "call_type",
        "outcome":        "call_outcome",  "status":       "deal_status",
        "duration":       "duration_mins", "remark":       "calling_remark",
        "remarks":        "calling_remark",
        "model":          "product_model", "config":       "configuration",
        "quantity":       "qty",           "required_quantity": "qty",
        "price":          "asking_price",  "quote":        "quote_given",
        "followup_date":  "next_followup_date",
        "next_followup":  "next_followup_date",
        "assigned":       "assigned_to",   "sales_person": "assigned_to",
        # Dealer-detail columns, so the Dealer Bulk Upload template can be fed
        # to this endpoint unchanged.
        "gst_number":     "gstin",         "gst":          "gstin",
        "gst_no":         "gstin",
        "contact":        "contact_person", "person_name": "contact_person",
        "owner_name":     "contact_person",
        "email_id":       "email",         "mail":         "email",
        "whatsapp":       "whatsapp_number",
        "pin":            "pincode",       "pin_code":     "pincode",
    }

    # Dealer columns this importer will create/update, csv key -> Dealer attr.
    # Only non-blank values are applied, so a sparse sheet never blanks out
    # detail that is already on record.
    _DEALER_COLS = {
        "business_name": "business_name", "contact_person": "contact_person",
        "phone": "phone", "email": "email", "city": "city",
        "state": "state", "pincode": "pincode", "address": "address",
        "gstin": "gstin", "dealer_type": "dealer_type",
        "whatsapp_number": "whatsapp_number", "assigned_to": "assigned_to",
    }

    # A row is a CALL only if it carries at least one of these. A row with just
    # dealer detail creates/updates the dealer and logs no call — otherwise
    # importing a dealer list would manufacture a blank call per dealer and
    # inflate every count on the Telecalling dashboard.
    _CALL_COLS = (
        "call_date", "call_type", "call_mode", "duration_mins", "call_outcome",
        "items_discussed", "quote_given", "next_followup_date", "notes",
        "calling_remark", "category", "product_model", "configuration",
        "qty", "asking_price", "deal_status",
    )

    reader = csv.DictReader(io.StringIO(text))
    raw_fields = reader.fieldnames or []
    reader.fieldnames = [
        _CALL_ALIASES.get(_norm(h), _norm(h)) for h in raw_fields
    ]
    rows = [
        {k: (str(v).strip() if v is not None else "") for k, v in r.items() if k}
        for r in reader
    ]
    if not rows:
        return RedirectResponse(url="/dealers?error=No+rows+found+in+file", status_code=302)

    # ── Resolve dealers set-based (two IN queries, not one query per row) ───
    want_phones = {r.get("dealer_phone", "").strip() for r in rows if r.get("dealer_phone", "").strip()}
    want_names = {r.get("business_name", "").strip().lower() for r in rows if r.get("business_name", "").strip()}

    by_phone: dict = {}
    by_name: dict = {}
    # Live dealers only — attaching a call log to a trashed dealer would file it
    # against a record nobody can see, so the call would look silently lost.
    if want_phones:
        res = await db.execute(select(Dealer).where(
            Dealer.phone.in_(want_phones), Dealer.trashed_at.is_(None)))
        by_phone = {d.phone: d for d in res.scalars().all()}
    if want_names:
        res = await db.execute(
            select(Dealer).where(
                func.lower(Dealer.business_name).in_(want_names),
                Dealer.trashed_at.is_(None),
            )
        )
        by_name = {(d.business_name or "").strip().lower(): d for d in res.scalars().all()}

    # ── Per-row parse helpers ──────────────────────────────────────────────
    def _to_date(val: str):
        if not val:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
        raise ValueError(f"unrecognised date '{val}'")

    def _to_int(val: str):
        return int(float(val)) if val else None

    def _to_dec(val: str):
        return Decimal(val.replace(",", "")) if val else None

    valid_types = {"outbound", "inbound"}
    valid_modes = {"phone", "whatsapp", "in_person"}

    inserted, errors = 0, []
    created_dealers, updated_dealers = 0, 0
    # MAX-based, not count(*)-based — see utils/dealer_code.
    alloc_code = await dealer_code_allocator(db)

    for i, row in enumerate(rows, start=2):  # row 1 = header
        try:
            phone = row.get("dealer_phone", "").strip()
            name = row.get("business_name", "").strip()
            dealer = by_phone.get(phone) if phone else None
            if dealer is None and name:
                dealer = by_name.get(name.lower())
            if dealer is None:
                # Unknown dealer: create it from this row rather than rejecting the
                # call. A telecaller's sheet IS the source of new leads — refusing
                # rows for dealers that don't exist yet meant the operator had to
                # import the same list twice, once per uploader, to get any of it in.
                if not (phone or name):
                    errors.append(f"Row {i}: needs a dealer_phone or a business_name")
                    continue
                dealer = Dealer(
                    dealer_code=alloc_code(),
                    business_name=name or (phone or "Unknown"),
                    phone=phone or None,
                    dealer_type="retail",
                    assigned_to=(row.get("assigned_to", "").strip() or None),
                    created_by=current_user.username,
                    added_by=current_user.username,
                    source="Call Records Bulk Upload",
                )
                # Any other dealer detail the row carries (city, email, GSTIN …)
                for col, attr in _DEALER_COLS.items():
                    val = (row.get(col, "") or "").strip()
                    if val:
                        setattr(dealer, attr, val)
                db.add(dealer)
                # flush so dealer.id exists for the DealerCall FK below, and so the
                # next row referencing the same phone/name reuses this one instead
                # of creating a second copy within the same file.
                await db.flush()
                if phone:
                    by_phone[phone] = dealer
                if name:
                    by_name[name.lower()] = dealer
                created_dealers += 1
            else:
                # Existing dealer: fill in / refresh whatever detail the row
                # carries. Blank cells are ignored so a call-only sheet never
                # wipes an address or email that is already on record.
                changed = False
                for col, attr in _DEALER_COLS.items():
                    val = (row.get(col, "") or "").strip()
                    if val and (getattr(dealer, attr, None) or "") != val:
                        setattr(dealer, attr, val)
                        changed = True
                if changed:
                    updated_dealers += 1

            # No call columns filled in -> this row is dealer detail only.
            # Logging an empty call here would put a blank row in the dealer's
            # history and inflate Total Calls on the Telecalling dashboard.
            if not any((row.get(c, "") or "").strip() for c in _CALL_COLS):
                continue

            call_type = (row.get("call_type", "").strip().lower() or "outbound")
            if call_type not in valid_types:
                call_type = "outbound"
            call_mode = (row.get("call_mode", "").strip().lower() or "phone")
            if call_mode not in valid_modes:
                call_mode = "phone"

            # called_by is the rep the row names, NOT whoever ran the upload.
            # The Telecalling Dashboard scopes Recent Calls and the agent KPI
            # rollup by called_by, so crediting the uploader would hide every
            # imported call from the rep who actually made it and pile the whole
            # file onto the admin account. Fall back to the uploader only when
            # the row names nobody.
            row_agent = (row.get("assigned_to", "") or "").strip()
            db.add(DealerCall(
                dealer_id=dealer.id,
                called_by=row_agent or current_user.username,
                call_date=_to_date(row.get("call_date", "").strip()) or app_now(),
                call_type=call_type,
                call_mode=call_mode,
                duration_mins=_to_int(row.get("duration_mins", "").strip()),
                call_outcome=row.get("call_outcome", "").strip().lower() or None,
                items_discussed=row.get("items_discussed", "").strip() or None,
                quote_given=_to_dec(row.get("quote_given", "").strip()),
                next_followup_date=_to_date(row.get("next_followup_date", "").strip()),
                notes=row.get("notes", "").strip() or None,
                calling_remark=row.get("calling_remark", "").strip() or None,
                category=row.get("category", "").strip() or None,
                product_model=row.get("product_model", "").strip() or None,
                configuration=row.get("configuration", "").strip() or None,
                qty=_to_int(row.get("qty", "").strip()),
                asking_price=_to_dec(row.get("asking_price", "").strip()),
                deal_status=row.get("deal_status", "").strip().lower() or None,
                assigned_to=row.get("assigned_to", "").strip() or None,
            ))
            inserted += 1
        except Exception as exc:
            errors.append(f"Row {i}: {exc}")

    await audit(db, action="DEALER_CALL_BULK_UPLOAD", user=current_user,
                table_name="dealer_calls", record_id="bulk",
                new_value={"inserted": inserted, "created_dealers": created_dealers,
                           "updated_dealers": updated_dealers,
                           "skipped": len(errors), "rows": len(rows)})
    await db.commit()

    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": " · ".join(
            ["Dealer Call Records"]
            + ([f"{created_dealers} dealer(s) created"] if created_dealers else [])
            + ([f"{updated_dealers} dealer(s) updated"] if updated_dealers else [])
            + [f"{inserted} call(s) logged"]),
        "inserted": inserted, "errors": errors,
        "back_url": "/dealers",
    })


@router.get("/new", response_class=HTMLResponse)
async def new_dealer_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    sales_result = await db.execute(
        select(User).where(User.role.in_([UserRole.sales, UserRole.sales_manager, UserRole.telecaller]), User.status == True)
    )
    sales_users = sales_result.scalars().all()
    return templates.TemplateResponse("dealers/form.html", {
        "request": request, "current_user": current_user,
        "dealer": None, "sales_users": sales_users,
        "dealer_type_options": await master_values(db, "dealer_dealer_type"),
        "dealer_status_options": await master_values(db, "dealer_status"),
    })


@router.post("/new")
async def create_dealer(
    request: Request,
    business_name: str = Form(...),
    first_name: str = Form(default=None),
    last_name: str = Form(default=None),
    contact_person: str = Form(default=None),
    dealer_type: str = Form(default="retail"),
    dealer_sub_category: str = Form(default=None),
    phone: str = Form(default=None),
    whatsapp_number: str = Form(default=None),
    email: str = Form(default=None),
    gstin: str = Form(default=None),
    address: str = Form(default=None),
    city: str = Form(default=None),
    state: str = Form(default=None),
    pincode: str = Form(default=None),
    website: str = Form(default=None),
    google_cid: str = Form(default=None),
    preferred_categories: str = Form(default=None),
    credit_limit: float = Form(default=0),
    assigned_to: str = Form(default=None),
    status: str = Form(default="active"),
    notes: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
    _perm: User = Depends(require_module_perm("dealers", "add")),
):
    # Auto-generate dealer code
    # Keeps this path's un-hyphenated DLR#### format, but numbers it from
    # MAX(code) instead of count(*). dealer_code is printed on paperwork, so the
    # shape must not change - only the collision-prone arithmetic behind it.
    dealer_code = await next_dealer_code(db, prefix="DLR")

    dealer = Dealer(
        dealer_code=dealer_code,
        business_name=business_name,
        first_name=first_name or None,
        last_name=last_name or None,
        contact_person=contact_person,
        dealer_type=dealer_type,
        dealer_sub_category=(dealer_sub_category or "").strip() or None,
        phone=phone,
        whatsapp_number=whatsapp_number,
        email=email,
        gstin=gstin,
        address=address,
        city=city,
        state=state,
        pincode=pincode,
        website=(website or "").strip() or None,
        google_cid=parse_google_cid(google_cid),
        preferred_categories=preferred_categories,
        credit_limit=credit_limit,
        assigned_to=assigned_to or current_user.username,
        status=status,
        notes=notes,
        created_by=current_user.username,
    )
    db.add(dealer)
    await db.commit()
    return RedirectResponse(url=f"/dealers/{dealer.id}?success=Dealer+created", status_code=302)


@router.post("/{dealer_id}/credit-notes/{cn_id}/apply")
async def apply_credit_note(
    request: Request,
    dealer_id: str,
    cn_id: str,
    _csrf: None = Depends(verify_csrf),
    order_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Apply a credit note balance against an open dealer order (reduces due_amount)."""

    import uuid as _uuid
    try:
        _uuid.UUID(str(cn_id))
        _uuid.UUID(str(dealer_id))
    except ValueError:
        return RedirectResponse(url="/dealers?error=Invalid+ID", status_code=302)

    # Fetch CN and verify it belongs to this dealer and is not yet applied
    cn_result = await db.execute(
        select(DealerCreditNote)
        .where(DealerCreditNote.id == cn_id, DealerCreditNote.dealer_id == dealer_id)
    )
    cn = cn_result.scalar_one_or_none()
    if not cn:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Credit+note+not+found", status_code=302)
    if cn.is_applied:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Credit+note+already+applied", status_code=302)

    # Validate order_id UUID before fetching
    try:
        _uuid.UUID(str(order_id))
    except ValueError:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Invalid+order+ID", status_code=302)

    # Fetch target order and verify it belongs to this dealer and has outstanding balance
    order_result = await db.execute(
        select(DealerOrder)
        .where(DealerOrder.id == order_id, DealerOrder.dealer_id == dealer_id)
    )
    order = order_result.scalar_one_or_none()
    if not order:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Order+not+found", status_code=302)
    if float(order.due_amount or 0) <= 0:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Order+has+no+outstanding+balance", status_code=302)

    # Apply: reduce due_amount by min(cn.amount, order.due_amount)
    from decimal import Decimal
    applied_amount = min(cn.amount, order.due_amount)
    order.due_amount = order.due_amount - applied_amount
    if order.due_amount < Decimal("0"):
        order.due_amount = Decimal("0")
    # Mark order as paid if fully settled
    if order.due_amount == Decimal("0") and order.status not in ("cancelled", "paid"):
        order.status = "paid"

    # Mark CN as applied
    cn.is_applied = True
    cn.applied_at = app_now()
    cn.applied_to_order_id = order.id

    # Audit before commit
    await audit(
        db,
        user=current_user,
        action="CREDIT_NOTE_APPLIED",
        table_name="dealer_credit_notes",
        record_id=cn.credit_number,
        notes=f"Applied ₹{applied_amount:,.0f} from CN {cn.credit_number} to order {order.order_number}; new due_amount=₹{float(order.due_amount):,.0f}",
        request=request,
    )

    await db.commit()
    return RedirectResponse(
        url=f"/dealers/{dealer_id}?success=Credit+note+applied+successfully",
        status_code=302,
    )


@router.get("/{dealer_id}/orders/{order_id}/invoice", response_class=HTMLResponse)
async def dealer_order_invoice(
    request: Request,
    dealer_id: str,
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Printable GST invoice for a dealer order."""

    import uuid as _uuid
    try:
        _uuid.UUID(str(dealer_id)); _uuid.UUID(str(order_id))
    except ValueError:
        return RedirectResponse(url="/dealers?error=Invalid+ID", status_code=302)

    dealer_result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = dealer_result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Dealer+not+found", status_code=302)

    order_result = await db.execute(
        select(DealerOrder).where(DealerOrder.id == order_id, DealerOrder.dealer_id == dealer_id)
    )
    order = order_result.scalar_one_or_none()
    if not order:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Order+not+found", status_code=302)

    if not order.invoice_number:
        order.invoice_number = f"INV-{order.order_number}"
        await db.commit()

    return templates.TemplateResponse("dealers/order_invoice.html", {
        "request": request,
        "current_user": current_user,
        "dealer": dealer,
        "order": order,
        "as_of": app_now(),
    })


@router.get("/{dealer_id}", response_class=HTMLResponse)
async def dealer_profile(
    request: Request,
    dealer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Dealer+not+found", status_code=302)

    calls_result = await db.execute(
        select(DealerCall).where(DealerCall.dealer_id == dealer.id)
        .order_by(DealerCall.call_date.desc()).limit(20)
    )
    calls = calls_result.scalars().all()

    orders_result = await db.execute(
        select(DealerOrder).where(DealerOrder.dealer_id == dealer.id)
        .order_by(DealerOrder.order_date.desc())
    )
    orders = orders_result.scalars().all()

    # Live outstanding: SUM of due_amount on non-cancelled orders
    outstanding_live = float((await db.execute(
        select(func.coalesce(func.sum(DealerOrder.due_amount), 0))
        .where(DealerOrder.dealer_id == dealer.id, DealerOrder.status.in_(OUTSTANDING_STATUSES))
    )).scalar() or 0)

    credit_notes = (await db.execute(
        select(DealerCreditNote)
        .options(
            selectinload(DealerCreditNote.order),
            selectinload(DealerCreditNote.applied_to_order),
        )
        .where(DealerCreditNote.dealer_id == dealer.id)
        .order_by(DealerCreditNote.created_at.desc())
    )).scalars().all()

    # Open orders eligible for credit-note application (due_amount > 0, non-cancelled)
    open_orders = [o for o in orders if float(o.due_amount or 0) > 0 and o.status in OUTSTANDING_STATUSES]

    quotations = (await db.execute(
        select(DealerQuotation).where(DealerQuotation.dealer_id == dealer.id)
        .order_by(DealerQuotation.created_at.desc())
    )).scalars().all()

    today = app_now().date()
    return templates.TemplateResponse("dealers/profile.html", {
        "request": request,
        "current_user": current_user,
        "dealer": dealer,
        "website_url": safe_external_url(dealer.website),
        "calls": calls,
        "orders": orders,
        "today": today,
        "outstanding_live": outstanding_live,
        "upsell_suggestions": _upsell_suggestions(dealer, outstanding_live),
        "credit_notes": credit_notes,
        "open_orders": open_orders,
        "quotations": quotations,
        "company_settings": await get_company_settings(db),
    })


@router.get("/{dealer_id}/statement.csv")
async def dealer_statement_csv(
    dealer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Download a complete account statement for a dealer as CSV."""
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Dealer+not+found", status_code=302)

    orders_result = await db.execute(
        select(DealerOrder)
        .where(DealerOrder.dealer_id == dealer.id)
        .order_by(DealerOrder.order_date)
    )
    orders = orders_result.scalars().all()

    cn_result = await db.execute(
        select(DealerCreditNote)
        .where(DealerCreditNote.dealer_id == dealer.id)
        .order_by(DealerCreditNote.credit_date)
    )
    credit_notes = cn_result.scalars().all()

    receipts_result = await db.execute(
        select(CustomerReceipt)
        .where(CustomerReceipt.dealer_id == dealer.id)
        .order_by(CustomerReceipt.receipt_date)
    )
    receipts = receipts_result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header block
    writer.writerow(["ACCOUNT STATEMENT"])
    writer.writerow(["Dealer", dealer.business_name])
    writer.writerow(["Code", dealer.dealer_code])
    writer.writerow(["Generated", app_now().strftime("%d-%m-%Y %H:%M UTC")])
    writer.writerow([])

    # Orders section
    writer.writerow(["ORDERS"])
    writer.writerow(["Order#", "Date", "Total (Rs)", "Paid (Rs)", "Due (Rs)", "Status"])
    total_orders = 0.0
    total_paid_orders = 0.0
    total_due_orders = 0.0
    for o in orders:
        total_val = float(o.total_amount or 0)
        paid_val  = float(o.paid_amount or 0)
        due_val   = float(o.due_amount or 0)
        total_orders      += total_val
        total_paid_orders += paid_val
        total_due_orders  += due_val
        writer.writerow([
            o.order_number,
            o.order_date.strftime("%d-%m-%Y"),
            f"{total_val:.2f}",
            f"{paid_val:.2f}",
            f"{due_val:.2f}",
            o.status,
        ])
    writer.writerow(["TOTAL", "", f"{total_orders:.2f}", f"{total_paid_orders:.2f}", f"{total_due_orders:.2f}", ""])
    writer.writerow([])

    # Credit notes section
    writer.writerow(["CREDIT NOTES"])
    writer.writerow(["Credit#", "Date", "Amount (Rs)", "Reason", "Items"])
    total_credits = 0.0
    for cn in credit_notes:
        cn_val = float(cn.amount or 0)
        total_credits += cn_val
        writer.writerow([
            cn.credit_number,
            cn.credit_date.strftime("%d-%m-%Y"),
            f"{cn_val:.2f}",
            (cn.reason or "").replace("_", " ").title(),
            cn.items_description or "",
        ])
    writer.writerow(["TOTAL CREDITS", "", f"{total_credits:.2f}", "", ""])
    writer.writerow([])

    # Receipts section
    writer.writerow(["RECEIPTS"])
    writer.writerow(["Date", "Amount (Rs)", "Mode", "Reference"])
    total_receipts = 0.0
    for r in receipts:
        r_val = float(r.amount or 0)
        total_receipts += r_val
        writer.writerow([
            r.receipt_date.strftime("%d-%m-%Y"),
            f"{r_val:.2f}",
            r.payment_mode or "",
            r.reference_no or "",
        ])
    writer.writerow(["TOTAL RECEIPTS", f"{total_receipts:.2f}", "", ""])
    writer.writerow([])

    # Summary
    writer.writerow(["SUMMARY"])
    writer.writerow(["Total Order Value", f"{total_orders:.2f}"])
    writer.writerow(["Total Credit Notes", f"{total_credits:.2f}"])
    writer.writerow(["Net Order Value", f"{total_orders - total_credits:.2f}"])
    writer.writerow(["Total Receipts", f"{total_receipts:.2f}"])
    writer.writerow(["Net Outstanding", f"{total_due_orders:.2f}"])

    content = output.getvalue()
    filename = f"statement_{dealer.dealer_code}_{date.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{dealer_id}/edit", response_class=HTMLResponse)
async def edit_dealer_form(
    request: Request,
    dealer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Not+found", status_code=302)
    sales_result = await db.execute(
        select(User).where(User.role.in_([UserRole.sales, UserRole.sales_manager, UserRole.telecaller]))
    )
    sales_users = sales_result.scalars().all()
    return templates.TemplateResponse("dealers/form.html", {
        "request": request, "current_user": current_user,
        "dealer": dealer, "sales_users": sales_users,
        "dealer_type_options": await master_values(db, "dealer_dealer_type"),
        "dealer_status_options": await master_values(db, "dealer_status"),
    })


@router.post("/{dealer_id}/edit")
async def update_dealer(
    request: Request,
    dealer_id: str,
    business_name: str = Form(...),
    first_name: str = Form(default=None),
    last_name: str = Form(default=None),
    contact_person: str = Form(default=None),
    dealer_type: str = Form(default="retail"),
    dealer_sub_category: str = Form(default=None),
    phone: str = Form(default=None),
    whatsapp_number: str = Form(default=None),
    email: str = Form(default=None),
    gstin: str = Form(default=None),
    address: str = Form(default=None),
    city: str = Form(default=None),
    state: str = Form(default=None),
    pincode: str = Form(default=None),
    website: str = Form(default=None),
    google_cid: str = Form(default=None),
    preferred_categories: str = Form(default=None),
    credit_limit: float = Form(default=0),
    assigned_to: str = Form(default=None),
    status: str = Form(default="active"),
    notes: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Not+found", status_code=302)
    dealer.business_name = business_name
    dealer.first_name = first_name or None
    dealer.last_name = last_name or None
    dealer.contact_person = contact_person
    dealer.dealer_type = dealer_type
    # Only fields this form actually posted are written, so an older cached copy
    # of the page (without the new inputs) cannot blank them.
    if dealer_sub_category is not None:
        dealer.dealer_sub_category = dealer_sub_category.strip() or None
    if website is not None:
        dealer.website = website.strip() or None
    if google_cid is not None:
        dealer.google_cid = parse_google_cid(google_cid)
    dealer.phone = phone
    dealer.whatsapp_number = whatsapp_number
    dealer.email = email
    dealer.gstin = gstin
    dealer.address = address
    dealer.city = city
    dealer.state = state
    dealer.pincode = pincode
    dealer.preferred_categories = preferred_categories
    dealer.credit_limit = credit_limit
    dealer.assigned_to = assigned_to
    dealer.status = status
    dealer.notes = notes
    await db.commit()
    return RedirectResponse(url=f"/dealers/{dealer_id}?success=Dealer+updated", status_code=302)


@router.post("/{dealer_id}/quick-edit")
async def quick_edit_dealer(
    dealer_id: str,
    business_name: str = Form(...),
    contact_person: str = Form(default=None),
    dealer_type: str = Form(default="retail"),
    dealer_sub_category: str = Form(default=None),
    phone: str = Form(default=None),
    email: str = Form(default=None),
    address: str = Form(default=None),
    city: str = Form(default=None),
    state: str = Form(default=None),
    status: str = Form(default="active"),
    call_id: str = Form(default=""),
    qty: str = Form(default=""),
    items_discussed: str = Form(default=""),
    calling_remark: str = Form(default=""),
    notes: str = Form(default=""),
    purchase_quantity: str = Form(default=""),
    call_mode: str = Form(default=""),
    call_type: str = Form(default=""),
    deals_in: str = Form(default=""),
    whom_to_sell: str = Form(default=""),
    next_followup_date: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Combined Edit modal on Dealer Management: updates the Dealer's core
    fields plus the most-recent DealerCall's call-log-sourced fields
    (creating a minimal DealerCall row if the dealer has none yet).

    Selling Quantity is intentionally not editable here (removed from this
    modal) — sale_quantity is left untouched so historical values captured via
    the full Log Call form (/dealers/{id}/call) aren't wiped on every quick
    edit."""
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Not+found", status_code=302)

    dealer.business_name = business_name
    dealer.contact_person = contact_person or None
    dealer.dealer_type = dealer_type
    dealer.phone = phone or None
    dealer.email = email or None
    dealer.address = address or None
    # Written only when the modal posted them (None = field not on the form),
    # so a modal without these inputs never blanks them.
    if dealer_sub_category is not None:
        dealer.dealer_sub_category = dealer_sub_category.strip() or None
    if city is not None:
        dealer.city = city.strip() or None
    if state is not None:
        dealer.state = state.strip() or None
    dealer.status = status

    call = None
    if call_id:
        call_result = await db.execute(select(DealerCall).where(DealerCall.id == call_id, DealerCall.dealer_id == dealer_id))
        call = call_result.scalar_one_or_none()
    if not call:
        call = DealerCall(dealer_id=dealer_id, called_by=current_user.username)
        db.add(call)

    call.qty = int(qty) if (qty or "").strip().isdigit() else None
    call.items_discussed = items_discussed or None
    call.calling_remark = calling_remark or None
    call.notes = notes or None
    call.purchase_quantity = int(purchase_quantity) if (purchase_quantity or "").strip().isdigit() else None
    if call_mode:
        call.call_mode = call_mode
    if call_type:
        call.call_type = call_type
    call.deals_in = deals_in or None
    call.whom_to_sell = whom_to_sell or None
    if next_followup_date:
        try:
            call.next_followup_date = datetime.strptime(next_followup_date, "%Y-%m-%d")
        except ValueError:
            pass

    await db.commit()
    return RedirectResponse(url="/dealers?success=Dealer+updated", status_code=302)


@router.post("/quick-new")
async def quick_new_dealer(
    business_name: str = Form(...),
    contact_person: str = Form(default=None),
    dealer_type: str = Form(default="retail"),
    dealer_sub_category: str = Form(default=None),
    phone: str = Form(default=None),
    email: str = Form(default=None),
    address: str = Form(default=None),
    city: str = Form(default=None),
    state: str = Form(default=None),
    status: str = Form(default="active"),
    qty: str = Form(default=""),
    items_discussed: str = Form(default=""),
    calling_remark: str = Form(default=""),
    notes: str = Form(default=""),
    purchase_quantity: str = Form(default=""),
    call_mode: str = Form(default=""),
    call_type: str = Form(default=""),
    deals_in: str = Form(default=""),
    whom_to_sell: str = Form(default=""),
    next_followup_date: str = Form(default=""),
    return_to: str = Form(default="/dealers"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
    _perm: User = Depends(require_module_perm("dealers", "add")),
):
    """Add Dealer modal on Dealer Management: creates a new Dealer using the
    same field set as the Edit Dealer modal, plus an initial DealerCall row
    if any call-log-sourced fields were filled in."""
    # Keeps this path's un-hyphenated DLR#### format, but numbers it from
    # MAX(code) instead of count(*). dealer_code is printed on paperwork, so the
    # shape must not change - only the collision-prone arithmetic behind it.
    dealer_code = await next_dealer_code(db, prefix="DLR")

    dealer = Dealer(
        dealer_code=dealer_code,
        business_name=business_name,
        contact_person=contact_person or None,
        dealer_type=dealer_type,
        dealer_sub_category=(dealer_sub_category or "").strip() or None,
        phone=phone or None,
        email=email or None,
        address=address or None,
        city=(city or "").strip() or None,
        state=(state or "").strip() or None,
        status=status,
        added_by=current_user.username,
        assigned_to=current_user.username,
    )
    db.add(dealer)
    await db.flush()

    has_call_data = any((qty, items_discussed, calling_remark, notes, purchase_quantity,
                          call_mode, call_type, deals_in, whom_to_sell, next_followup_date))
    if has_call_data:
        call = DealerCall(dealer_id=dealer.id, called_by=current_user.username)
        call.qty = int(qty) if (qty or "").strip().isdigit() else None
        call.items_discussed = items_discussed or None
        call.calling_remark = calling_remark or None
        call.notes = notes or None
        call.purchase_quantity = int(purchase_quantity) if (purchase_quantity or "").strip().isdigit() else None
        if call_mode:
            call.call_mode = call_mode
        if call_type:
            call.call_type = call_type
        call.deals_in = deals_in or None
        call.whom_to_sell = whom_to_sell or None
        if next_followup_date:
            try:
                call.next_followup_date = datetime.strptime(next_followup_date, "%Y-%m-%d")
            except ValueError:
                pass
        db.add(call)

    await db.commit()
    safe_return = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/dealers"
    sep = "&" if "?" in safe_return else "?"
    return RedirectResponse(url=f"{safe_return}{sep}success=Dealer+{dealer_code}+created", status_code=302)


@router.get("/{dealer_id}/call", response_class=HTMLResponse)
async def call_form(
    request: Request,
    dealer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers", status_code=302)
    su_result = await db.execute(
        select(User).where(
            User.role.in_([UserRole.sales, UserRole.sales_manager, UserRole.telecaller]),
            User.status == True,
        ).order_by(User.full_name)
    )
    return templates.TemplateResponse("dealers/call_form.html", {
        "request": request, "current_user": current_user, "dealer": dealer,
        "sales_users": su_result.scalars().all(),
        "tc_options": await _tc_field_options(db),
        "call_mode_options": await master_values(db, "call_mode"),
        "call_type_options": await master_values(db, "call_type"),
        "call_outcome_options": await master_values(db, "call_outcome"),
    })


@router.post("/{dealer_id}/call")
async def log_call(
    request: Request,
    dealer_id: str,
    call_mode: str = Form(default="phone"),
    call_type: str = Form(default="outbound"),
    duration_mins: str = Form(default=None),
    call_outcome: str = Form(default=None),
    next_followup_date: str = Form(default=None),
    next_followup_time: str = Form(default=None),
    items_discussed: str = Form(default=None),
    quote_given: str = Form(default=None),
    whatsapp_sent: str = Form(default=None),
    notes: str = Form(default=None),
    calling_remark: str = Form(default=None),
    category: str = Form(default=None),
    product_model: str = Form(default=None),
    configuration: str = Form(default=None),
    qty: str = Form(default=None),
    monthly_quantity: str = Form(default=None),
    asking_price: str = Form(default=None),
    deal_status: str = Form(default=None),
    requirements_preferred_config: str = Form(default=None),
    whom_to_sell: str = Form(default=None),
    deals_in: str = Form(default=None),
    stock_type: str = Form(default=None),
    assigned_to: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    next_dt = _combine_date_time(next_followup_date, next_followup_time)

    _duration = int(duration_mins) if duration_mins and duration_mins.strip() else None
    _quote = float(quote_given) if quote_given and quote_given.strip() else None
    _qty = int(qty) if qty and qty.strip().isdigit() else None
    _monthly_qty = int(monthly_quantity) if monthly_quantity and monthly_quantity.strip().isdigit() else None
    _asking_price = float(asking_price) if asking_price and asking_price.strip() else None

    call = DealerCall(
        dealer_id=dealer_id,
        called_by=current_user.username,
        call_mode=call_mode,
        call_type=call_type,
        duration_mins=_duration,
        call_outcome=call_outcome,
        next_followup_date=next_dt,
        items_discussed=items_discussed,
        quote_given=_quote,
        whatsapp_sent=bool(whatsapp_sent),
        notes=notes,
        calling_remark=(calling_remark or "").strip() or None,
        category=(category or "").strip() or None,
        product_model=(product_model or "").strip() or None,
        configuration=(configuration or "").strip() or None,
        qty=_qty,
        monthly_quantity=_monthly_qty,
        asking_price=_asking_price,
        deal_status=(deal_status or "").strip() or None,
        requirements_preferred_config=(requirements_preferred_config or "").strip() or None,
        whom_to_sell=(whom_to_sell or "").strip() or None,
        deals_in=(deals_in or "").strip() or None,
        stock_type=(stock_type or "").strip() or None,
        assigned_to=(assigned_to or "").strip() or None,
    )
    db.add(call)

    # Update dealer last_sale_date if order placed
    if call_outcome == 'order_placed':
        result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
        dealer = result.scalar_one_or_none()
        if dealer:
            dealer.last_sale_date = app_now()

    await db.commit()
    return RedirectResponse(url=f"/dealers/{dealer_id}?success=Call+logged", status_code=302)


# ── EDIT CALL LOG ─────────────────────────────────────────────────────────────

@router.get("/{dealer_id}/calls/{call_id}/edit", response_class=HTMLResponse)
async def edit_call_form(
    request: Request,
    dealer_id: str,
    call_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    dealer = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers", status_code=302)
    call = (await db.execute(
        select(DealerCall).where(DealerCall.id == call_id, DealerCall.dealer_id == dealer.id)
    )).scalar_one_or_none()
    if not call:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Call+not+found", status_code=302)
    su_result = await db.execute(
        select(User).where(
            User.role.in_([UserRole.sales, UserRole.sales_manager, UserRole.telecaller]),
            User.status == True,
        ).order_by(User.full_name)
    )
    return templates.TemplateResponse("dealers/call_form.html", {
        "request": request, "current_user": current_user,
        "dealer": dealer, "edit_call": call,
        "sales_users": su_result.scalars().all(),
        "tc_options": await _tc_field_options(db),
        "call_mode_options": await master_values(db, "call_mode"),
        "call_type_options": await master_values(db, "call_type"),
        "call_outcome_options": await master_values(db, "call_outcome"),
    })


@router.post("/{dealer_id}/calls/{call_id}/edit")
async def update_call(
    request: Request,
    dealer_id: str,
    call_id: str,
    call_mode: str = Form(default="phone"),
    call_type: str = Form(default="outbound"),
    call_date: str = Form(default=None),
    duration_mins: str = Form(default=None),
    call_outcome: str = Form(default=None),
    next_followup_date: str = Form(default=None),
    next_followup_time: str = Form(default=None),
    items_discussed: str = Form(default=None),
    quote_given: str = Form(default=None),
    whatsapp_sent: str = Form(default=None),
    notes: str = Form(default=None),
    calling_remark: str = Form(default=None),
    category: str = Form(default=None),
    product_model: str = Form(default=None),
    configuration: str = Form(default=None),
    qty: str = Form(default=None),
    monthly_quantity: str = Form(default=None),
    asking_price: str = Form(default=None),
    deal_status: str = Form(default=None),
    requirements_preferred_config: str = Form(default=None),
    whom_to_sell: str = Form(default=None),
    deals_in: str = Form(default=None),
    stock_type: str = Form(default=None),
    assigned_to: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    call = (await db.execute(
        select(DealerCall).where(DealerCall.id == call_id, DealerCall.dealer_id == dealer_id)
    )).scalar_one_or_none()
    if not call:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Call+not+found", status_code=302)

    # Parse dates
    next_dt = _combine_date_time(next_followup_date, next_followup_time)

    call_dt = call.call_date
    if call_date and call_date.strip():
        try:
            call_dt = datetime.fromisoformat(call_date)
        except ValueError:
            pass

    call.call_mode = call_mode
    call.call_type = call_type
    call.call_date = call_dt
    call.duration_mins = int(duration_mins) if duration_mins and duration_mins.strip().isdigit() else None
    call.call_outcome = call_outcome or None
    call.next_followup_date = next_dt
    call.items_discussed = (items_discussed or "").strip() or None
    call.quote_given = float(quote_given) if quote_given and quote_given.strip() else None
    call.whatsapp_sent = bool(whatsapp_sent)
    call.notes = (notes or "").strip() or None
    call.calling_remark = (calling_remark or "").strip() or None
    call.category = (category or "").strip() or None
    call.product_model = (product_model or "").strip() or None
    call.configuration = (configuration or "").strip() or None
    call.qty = int(qty) if qty and qty.strip().isdigit() else None
    # purchase_quantity / sale_quantity are no longer collected on this form
    # (removed per the Dealer Call Log field revision) — deliberately left
    # untouched here so historical values captured via the old form (or the
    # Dealer Management quick-edit modal) are not silently wiped on save.
    call.monthly_quantity = int(monthly_quantity) if monthly_quantity and monthly_quantity.strip().isdigit() else None
    call.asking_price = float(asking_price) if asking_price and asking_price.strip() else None
    call.deal_status = (deal_status or "").strip() or None
    call.requirements_preferred_config = (requirements_preferred_config or "").strip() or None
    call.whom_to_sell = (whom_to_sell or "").strip() or None
    call.deals_in = (deals_in or "").strip() or None
    call.stock_type = (stock_type or "").strip() or None
    call.assigned_to = (assigned_to or "").strip() or None

    await db.commit()
    return RedirectResponse(url=f"/dealers/{dealer_id}?success=Call+updated", status_code=302)


@router.get("/{dealer_id}/orders/new", response_class=HTMLResponse)
async def new_order_form(
    request: Request,
    dealer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Dealer+not+found", status_code=302)
    count_r = await db.execute(select(func.count(DealerOrder.id)))
    n = (count_r.scalar() or 0) + 1
    order_number = f"DO-{app_now().year}-{n:04d}"
    return templates.TemplateResponse("dealers/order_form.html", {
        "request": request, "current_user": current_user,
        "dealer": dealer, "order_number": order_number,
        "today": date.today().isoformat(),
    })


@router.post("/{dealer_id}/orders/new")
async def create_order(
    request: Request,
    dealer_id: str,
    order_number: str = Form(...),
    order_date: str = Form(...),
    items_description: str = Form(default=""),
    total_amount: str = Form(default="0"),
    paid_amount: str = Form(default="0"),
    payment_due_date: str = Form(default=""),
    payment_mode: str = Form(default=""),
    invoice_number: str = Form(default=""),
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    try:
        total = float(total_amount) if total_amount.strip() else 0.0
        paid  = float(paid_amount)  if paid_amount.strip()  else 0.0
    except ValueError:
        return RedirectResponse(url=f"/dealers/{dealer_id}/orders/new?error=Invalid+amount", status_code=302)
    due = max(0.0, round(total - paid, 2))
    due_dt = None
    if payment_due_date.strip():
        try:
            due_dt = datetime.fromisoformat(payment_due_date)
        except ValueError:
            pass
    order = DealerOrder(
        dealer_id=dealer_id,
        order_number=order_number,
        order_date=datetime.fromisoformat(order_date) if order_date else app_now(),
        items_description=items_description or None,
        total_amount=total,
        paid_amount=paid,
        due_amount=due,
        payment_due_date=due_dt,
        payment_mode=payment_mode or None,
        invoice_number=invoice_number or None,
        status="paid" if due == 0 else "pending",
        notes=notes or None,
        created_by=current_user.username,
    )
    db.add(order)
    dr = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = dr.scalar_one_or_none()
    if dealer:
        dealer.total_purchases    = float(dealer.total_purchases    or 0) + total
        dealer.last_sale_date     = app_now()
        dealer.last_sale_amount   = total
    await audit(db, user=current_user, action="ORDER_CREATED",
                table_name="dealer_orders", record_id=str(order.id),
                new_value={"order_number": order_number, "dealer_id": dealer_id,
                           "total_amount": total, "paid_amount": paid,
                           "due_amount": due,
                           "status": "paid" if due == 0 else "pending"},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/dealers/{dealer_id}?success=Order+created", status_code=302)


@router.post("/{dealer_id}/orders/{order_id}/pay")
async def record_order_payment(
    request: Request,
    dealer_id: str,
    order_id: str,
    amount: str = Form(...),
    payment_mode: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError
    except ValueError:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Invalid+amount", status_code=302)
    or_r = await db.execute(select(DealerOrder).where(DealerOrder.id == order_id))
    order = or_r.scalar_one_or_none()
    if not order:
        return RedirectResponse(url=f"/dealers/{dealer_id}?error=Order+not+found", status_code=302)
    order.paid_amount = round(float(order.paid_amount or 0) + amt, 2)
    order.due_amount  = max(0.0, round(float(order.total_amount or 0) - float(order.paid_amount), 2))
    if order.due_amount == 0:
        order.status = "paid"
    if payment_mode:
        order.payment_mode = payment_mode
    await audit(db, user=current_user, action="PAYMENT_RECORDED",
                table_name="dealer_orders", record_id=str(order.id),
                new_value={"payment_amount": amt,
                           "payment_mode": payment_mode or None,
                           "order_id": str(order.id),
                           "dealer_id": dealer_id,
                           "new_paid_amount": float(order.paid_amount),
                           "new_due_amount": float(order.due_amount),
                           "new_status": order.status},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/dealers/{dealer_id}?success=Payment+recorded", status_code=302)


@router.post("/{dealer_id}/orders/{order_id}/credit-note")
async def create_credit_note(
    request: Request,
    dealer_id: str,
    order_id: str,
    _csrf: None = Depends(verify_csrf),
    amount: str = Form(...),
    reason: str = Form(default=""),
    items_description: str = Form(default=""),
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Issue a credit note against a dealer order (goods return / adjustment)."""
    # Load order and verify ownership
    order_result = await db.execute(
        select(DealerOrder)
        .where(DealerOrder.id == order_id, DealerOrder.dealer_id == dealer_id)
    )
    order = order_result.scalar_one_or_none()
    if not order:
        return RedirectResponse(
            url=f"/dealers/{dealer_id}?error=Order+not+found", status_code=302
        )

    # Validate amount
    try:
        credit_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        if credit_amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return RedirectResponse(
            url=f"/dealers/{dealer_id}?error=Invalid+credit+amount", status_code=302
        )
    if credit_amount > order.total_amount:
        return RedirectResponse(
            url=f"/dealers/{dealer_id}?error=Credit+exceeds+order+total", status_code=302
        )

    # Generate credit number: CN-YYYY-NNNN
    # Use MAX to find the last issued sequence — more resilient than COUNT under concurrent inserts
    year = app_now().year
    max_result = await db.execute(
        select(func.max(
            func.cast(
                func.split_part(DealerCreditNote.credit_number, "-", 3),
                Integer
            )
        ))
        .where(func.extract("year", DealerCreditNote.created_at) == year)
    )
    last_seq = max_result.scalar() or 0
    credit_number = f"CN-{year}-{last_seq + 1:04d}"

    # Create credit note record
    cn = DealerCreditNote(
        credit_number=credit_number,
        dealer_id=order.dealer_id,
        order_id=order.id,
        amount=credit_amount,
        reason=reason or None,
        items_description=items_description or None,
        notes=notes or None,
        created_by=current_user.username,
    )
    db.add(cn)

    # Adjust order totals
    new_total = max(Decimal("0"), Decimal(str(order.total_amount)) - credit_amount)
    new_due = max(Decimal("0"), new_total - Decimal(str(order.paid_amount)))
    order.total_amount = new_total
    order.due_amount = new_due
    if new_total <= 0:
        order.status = "cancelled"
    elif new_due == 0:
        order.status = "paid"
    # else: keep current status (pending / confirmed / delivered)

    # Audit
    await audit(
        db, user=current_user, action="CREDIT_NOTE_ISSUED",
        table_name="dealer_credit_notes", record_id=credit_number,
        new_value={
            "credit_number": credit_number,
            "order_id": str(order.id),
            "dealer_id": str(order.dealer_id),
            "credit_amount": float(credit_amount),
            "new_order_total": float(new_total),
            "new_order_due": float(new_due),
            "new_order_status": order.status,
            "reason": reason or None,
        },
        request=request,
    )
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        # Most likely a duplicate credit_number under concurrent requests — retry with next seq
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return RedirectResponse(
                url=f"/dealers/{dealer_id}?error=Please+try+again+(concurrent+request)", status_code=302
            )
        raise
    return RedirectResponse(
        url=f"/dealers/{dealer_id}?success=Credit+note+{credit_number}+issued",
        status_code=302,
    )


@router.get("/{dealer_id}/ledger", response_class=HTMLResponse)
async def dealer_ledger(
    request: Request,
    dealer_id: str,
    db: AsyncSession = Depends(get_db),
    # Stays Admin / Sales Manager: the ledger is a financial view, outside the
    # dealer-record management that Attendance Group managers were granted.
    current_user: User = Depends(require_sales_mgr),
):
    dr = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = dr.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/dealers?error=Not+found", status_code=302)

    orders_r = await db.execute(
        select(DealerOrder).where(DealerOrder.dealer_id == dealer_id).order_by(DealerOrder.order_date)
    )
    orders = orders_r.scalars().all()

    receipts_r = await db.execute(
        select(CustomerReceipt).where(CustomerReceipt.dealer_id == dealer_id).order_by(CustomerReceipt.receipt_date)
    )
    receipts = receipts_r.scalars().all()

    entries = []
    for o in orders:
        od = o.order_date
        odt = datetime(od.year, od.month, od.day) if not isinstance(od, datetime) else od
        entries.append({
            "date": odt,
            "ref": o.order_number,
            "description": f"Order — {(o.items_description or '')[:60]}",
            "debit": float(o.total_amount or 0),
            "credit": 0.0,
            "type": "order",
        })
    for r in receipts:
        rd = r.receipt_date
        dt = datetime(rd.year, rd.month, rd.day) if not isinstance(rd, datetime) else rd
        entries.append({
            "date": dt,
            "ref": r.reference_no or "—",
            "description": f"Payment received ({r.payment_mode or '—'})",
            "debit": 0.0,
            "credit": float(r.amount or 0),
            "type": "receipt",
        })

    entries.sort(key=lambda x: x["date"])

    balance = 0.0
    for e in entries:
        balance = round(balance + e["debit"] - e["credit"], 2)
        e["balance"] = balance

    total_orders   = sum(e["debit"]  for e in entries)
    total_receipts = sum(e["credit"] for e in entries)
    outstanding    = round(total_orders - total_receipts, 2)

    return templates.TemplateResponse("dealers/ledger.html", {
        "request": request, "current_user": current_user,
        "dealer": dealer, "entries": entries,
        "total_orders": total_orders, "total_receipts": total_receipts,
        "outstanding": outstanding,
    })
