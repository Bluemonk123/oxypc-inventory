from templates_config import templates
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import extract, select, func, text as sa_text
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, STAGE_LABELS, DROPDOWN_STAGES
from models.location import ZONE_LABELS, ZoneType
from models.engines import RepairAttempt
from models.lot import Lot
from models.sales import Sale
from models.stock_transfer import StockTransfer
from models.spare_parts import SparePartConsumption
from models.business_pl_override import BusinessPLOverride
from services.audit_engine import audit
from services.business_pl import compute_year_parts_labour_cogs, compute_year_device_cogs, compute_year_revenue
from utils.master_data import report_year_values, master_options
from auth.dependencies import get_current_user, require_roles, verify_csrf

# CSV dates (2026-10-21): year first ("2026-10-13 10:18"). Excel reads
# "13-10-2026" by the PC's own locale — on a month-first PC "01-10-2026"
# became 10 January and any day above 12 stayed text, so date filters and
# sorting in the export were wrong. Year-first is read the same everywhere.
def _csv_date(d) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def _csv_datetime(d) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else ""


# Maximum rows returned by any CSV export endpoint — prevents OOM on large datasets
MAX_EXPORT_ROWS = 5_000

# Financial reports — restricted to management/senior roles only
_REPORT_ROLES = require_roles(
    UserRole.inventory_manager,
    UserRole.qc_inspector,
    UserRole.sales,
)

# Receivables — sales management + inventory; admin always granted by require_roles()
_require_receivables = require_roles(
    UserRole.sales_manager,
    UserRole.inventory_manager,
)

# Company financials (P&L, lot P&L) — management only. The router-level
# _REPORT_ROLES gate is deliberately broad (it covers operational reports like
# stock-aging for sales/QC), so these two financial endpoints add a stricter
# per-route gate: sales reps and QC inspectors must NOT see company revenue/
# COGS/margins. (Flagged in the 2026-07-15 RBAC audit.)
_require_financials = require_roles(
    UserRole.inventory_manager,
    UserRole.sales_manager,
)
from services.daily_stock_reports import list_reports

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(_REPORT_ROLES)],
)


@router.get("/lot-pl", response_class=HTMLResponse)
async def lot_pl_report(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(_require_financials)):
    """Per-lot P&L (2026-10-16 rework):
    - trashed lots and trashed devices are left out everywhere;
    - Revenue is NET of customer returns (refund amount, or the sale price
      when no refund amount was recorded; rejected returns don't count);
    - Cost of Sold Units = buying cost of the sold devices (device_price, else
      the lot's buying price split evenly over its devices) + parts + labour
      booked on those sold devices, so a part-sold lot shows a fair
      Profit on Sold Units next to the whole-lot Profit."""
    from models.sales import Return
    live_dev = Device.is_trashed.isnot(True)
    lots = (await db.execute(
        select(Lot).where(Lot.is_trashed.isnot(True)).order_by(Lot.created_at.desc())
    )).scalars().all()

    # Gross revenue per lot
    rev_by_lot = {str(r.lot_id): float(r.revenue) for r in await db.execute(
        select(Device.lot_id, func.coalesce(func.sum(Sale.sale_price), 0).label("revenue"))
        .join(Sale, Sale.device_id == Device.id).where(live_dev)
        .group_by(Device.lot_id))}

    # Returns per lot: refund amount, falling back to the sale price
    ret_by_lot = {str(r.lot_id): float(r.returned) for r in await db.execute(
        select(Device.lot_id,
               func.coalesce(func.sum(func.coalesce(Return.refund_amount, Sale.sale_price)), 0).label("returned"))
        .join(Return, Return.device_id == Device.id)
        .join(Sale, Sale.id == Return.sale_id)
        .where(live_dev, func.coalesce(Return.approval_status, "pending") != "rejected")
        .group_by(Device.lot_id))}

    # Parts cost per lot (booked to the lot), and per sold device
    parts_by_lot = {str(r.lot_id): float(r.parts) for r in await db.execute(
        select(SparePartConsumption.lot_id, func.coalesce(func.sum(SparePartConsumption.total_cost), 0).label("parts"))
        .where(SparePartConsumption.lot_id.isnot(None))
        .group_by(SparePartConsumption.lot_id))}
    sold_parts_by_lot = {str(r.lot_id): float(r.parts) for r in await db.execute(
        select(Device.lot_id, func.coalesce(func.sum(SparePartConsumption.total_cost), 0).label("parts"))
        .join(SparePartConsumption, SparePartConsumption.device_id == Device.id)
        .where(live_dev, Device.current_stage == DeviceStage.sold)
        .group_by(Device.lot_id))}

    # Labour per lot, and per sold device
    labour_by_lot = {str(r.lot_id): float(r.labour) for r in await db.execute(
        select(Device.lot_id, func.coalesce(func.sum(RepairAttempt.cost), 0).label("labour"))
        .join(RepairAttempt, RepairAttempt.device_id == Device.id).where(live_dev)
        .group_by(Device.lot_id))}
    sold_labour_by_lot = {str(r.lot_id): float(r.labour) for r in await db.execute(
        select(Device.lot_id, func.coalesce(func.sum(RepairAttempt.cost), 0).label("labour"))
        .join(RepairAttempt, RepairAttempt.device_id == Device.id)
        .where(live_dev, Device.current_stage == DeviceStage.sold)
        .group_by(Device.lot_id))}

    # Device counts, sold counts, and buying cost of sold devices
    count_by_lot, sold_by_lot, sold_priced, sold_unpriced = {}, {}, {}, {}
    for r in await db.execute(
        select(Device.lot_id,
               func.count(Device.id).label("cnt"),
               func.count(Device.id).filter(Device.current_stage == DeviceStage.sold).label("sold"),
               func.coalesce(func.sum(Device.device_price).filter(Device.current_stage == DeviceStage.sold), 0).label("sold_price"),
               func.count(Device.id).filter(Device.current_stage == DeviceStage.sold,
                                            Device.device_price.is_(None)).label("sold_unpriced"))
        .where(live_dev).group_by(Device.lot_id)):
        lid = str(r.lot_id)
        count_by_lot[lid], sold_by_lot[lid] = r.cnt, r.sold
        sold_priced[lid], sold_unpriced[lid] = float(r.sold_price), r.sold_unpriced

    # Last activity per lot (2026-10-16): newest device change (stage moves,
    # sales, edits all bump Device.updated_at); the table sorts on this.
    dev_upd_by_lot = {str(r.lot_id): r.upd for r in await db.execute(
        select(Device.lot_id, func.max(Device.updated_at).label("upd"))
        .where(live_dev).group_by(Device.lot_id))}

    lot_pl = []
    for lot in lots:
        lid     = str(lot.id)
        gross   = rev_by_lot.get(lid, 0.0)
        returns = ret_by_lot.get(lid, 0.0)
        revenue = gross - returns
        parts   = parts_by_lot.get(lid, 0.0)
        labour  = labour_by_lot.get(lid, 0.0)
        buying  = float(lot.buying_price or 0)
        devices = count_by_lot.get(lid, 0)
        total_cost = buying + parts + labour
        profit  = revenue - total_cost
        margin  = round(profit / revenue * 100, 1) if revenue > 0 else 0
        per_unit = buying / devices if devices else 0.0
        sold_buy = sold_priced.get(lid, 0.0) + sold_unpriced.get(lid, 0) * per_unit
        cost_sold = sold_buy + sold_parts_by_lot.get(lid, 0.0) + sold_labour_by_lot.get(lid, 0.0)
        profit_sold = revenue - cost_sold
        lot_pl.append({
            "lot_number":   lot.lot_number,
            "supplier":     lot.supplier_name,
            "purchase_date":lot.purchase_date,
            "qty":          lot.qty,
            "devices":      devices,
            "sold":         sold_by_lot.get(lid, 0),
            "buying_price": buying,
            "parts_cost":   parts,
            "labour_cost":  labour,
            "total_cost":   total_cost,
            "gross_revenue":gross,
            "returns":      returns,
            "revenue":      revenue,
            "profit":       profit,
            "margin":       margin,
            "cost_sold":    cost_sold,
            "profit_sold":  profit_sold,
            "lot_id":       lid,
            "last_updated": max([d for d in (lot.created_at, dev_upd_by_lot.get(lid)) if d] or [None]),
        })
    return templates.TemplateResponse("reports/lot_pl.html", {
        "request": request, "lot_pl": lot_pl, "current_user": current_user
    })


# Named QA/test accounts to drop whenever "Exclude Admin User" is checked,
# on top of anyone with role='admin' — test_user/test_man aren't admin-role
# (l1_engineer/cosmetic_manager respectively) so the role filter alone
# wouldn't catch them. Fixed constant list, not user input — safe to inline
# into the SQL below.
_EXCLUDED_DAILY_STOCK_USERNAMES = ("admin", "test_user", "test_man")


def _stock_reconstruction_sql(exclude_admin: bool, as_of_param: str = "as_of") -> str:
    """Shared DISTINCT ON reconstruction — see _stock_as_of's docstring.
    `exclude_admin` drops any StageMovement performed by an admin-role user,
    or by one of _EXCLUDED_DAILY_STOCK_USERNAMES, from consideration
    entirely (as if it never happened), so a device whose only history
    before `as_of` was an admin/test action is reconstructed as
    not-yet-existing at that point rather than sitting in whatever stage
    that action left it in. Added for exactly that case: QA/test movements
    skewing the real operational numbers.

    A move INTO "sold" is timed by its Sale's sold_at (the Sale Date the user
    picked), not by when it was keyed in: the movement note carries the
    sale number ("Sold — SALE-1234"). A sale back-dated to 3 Oct but recorded on
    6 Oct is therefore a 3 Oct sale here, matching the Sales list — before
    this (2026-10-07) 772 sales dated 1-5 Oct were counted as sold on 6 Oct.
    The back-dated time is only used when it is EARLIER than the entry time, and
    never earlier than the tag's previous movement (a tag cannot be sold before
    it reached the stage it was sold from — otherwise a back-dated sale would
    sort BEFORE that earlier move and the tag would reappear in stock)."""
    admin_join = "LEFT JOIN users u ON u.username = sm.moved_by" if exclude_admin else ""
    if exclude_admin:
        excluded_list = ", ".join(f"'{u}'" for u in _EXCLUDED_DAILY_STOCK_USERNAMES)
        admin_filter = f"AND COALESCE(u.role, '') != 'admin' AND sm.moved_by NOT IN ({excluded_list})"
    else:
        admin_filter = ""
    return f"""
        SELECT DISTINCT ON (ev.device_id) ev.device_id, ev.to_stage
        FROM (
            SELECT sm.device_id, sm.to_stage, sm.moved_at,
                   CASE WHEN sm.to_stage = 'sold' AND sl.sold_at IS NOT NULL AND sl.sold_at < sm.moved_at
                        THEN GREATEST(sl.sold_at,
                                      COALESCE(LAG(sm.moved_at) OVER (PARTITION BY sm.device_id
                                                                      ORDER BY sm.moved_at), sl.sold_at))
                        ELSE sm.moved_at END AS ev_at
            FROM stage_movements sm
            {admin_join}
            LEFT JOIN sales sl ON sm.to_stage = 'sold'
                              AND sl.sale_number = substring(sm.notes from 'SALE-[0-9]+')
            WHERE 1 = 1
            {admin_filter}
        ) ev
        WHERE ev.ev_at <= :{as_of_param}
        ORDER BY ev.device_id, ev.ev_at DESC, ev.moved_at DESC
    """


# Stages that are not "stock on hand": a sold tag has left, and "GRN Receipt" rows
# duplicate the IQC / Stock In record of the same tag (they are receipt paperwork).
_NOT_STOCK_STAGES = ("sold", "grn")


def _stock_scope_sql(exclude_admin: bool, open_param: str | None) -> str:
    """Extra WHERE for a query aliasing the reconstruction as `latest`:
    drops sold / GRN rows, and — when `open_param` is given (the Closing
    snapshot) — keeps only tags that were already in stock at the day's opening,
    so Closing = Opening - what was sold that day (tags first received during the
    day are not counted: the GRN count is excluded)."""
    not_stock = ", ".join(f"'{x}'" for x in _NOT_STOCK_STAGES)
    # Deactivated / trashed tags are not stock anywhere else in the app (All
    # Inventory, Dashboard), so they are not counted here at any date either
    # (2026-10-07: 1,482 of them made Daily Stock 1.8k higher than All Inventory).
    clause = f"AND latest.to_stage NOT IN ({not_stock}) AND d.is_active AND NOT d.is_trashed"
    if open_param:
        clause += f"""
        AND latest.device_id IN (
            SELECT o.device_id
            FROM ({_stock_reconstruction_sql(exclude_admin, open_param)}) o
            JOIN devices od ON od.id = o.device_id
            WHERE o.to_stage NOT IN ({not_stock})
              AND od.is_active AND NOT od.is_trashed
              AND (od.trashed_at IS NULL OR od.trashed_at > :{open_param}))"""
    return clause


def _bind(as_of: datetime, open_as_of: datetime | None) -> dict:
    params = {"as_of": as_of}
    if open_as_of is not None:
        params["open_as_of"] = open_as_of
    return params


def _entity_filter_clause(entities: list | None) -> str:
    """Appended to any query already aliasing devices as `d` — `entities`
    comes from a server-rendered multi-select (master_options('entity')),
    never free text, so inlining the escaped values is safe the same way
    _EXCLUDED_DAILY_STOCK_USERNAMES is."""
    if not entities:
        return ""
    quoted = ", ".join("'" + e.replace("'", "''") + "'" for e in entities)
    return f"AND d.entity IN ({quoted})"


async def _stock_as_of(db: AsyncSession, as_of: datetime, exclude_admin: bool = False,
                        entities: list | None = None, open_as_of: datetime | None = None) -> dict:
    """Count of devices by stage, reconstructed as of a point in time —
    the most recent StageMovement.to_stage at or before `as_of` per device
    (SQL DISTINCT ON, Postgres-only like the rest of this app). This reads
    what was actually recorded rather than relying on StageMovement.exited_at
    intervals, so it stays correct even where some stage-change code path
    left exited_at unset on the previous row.

    A device trashed by `as_of` is excluded — same "don't count trashed
    stock" rule every other count in this app follows — but one trashed
    *after* `as_of` still counts, since it genuinely held that stage at the
    time being reconstructed. `entities` optionally restricts to one or more
    Device.entity values (the Entity multi-select on the page)."""
    rows = (await db.execute(sa_text(f"""
        SELECT latest.to_stage, count(*)
        FROM ({_stock_reconstruction_sql(exclude_admin)}) latest
        JOIN devices d ON d.id = latest.device_id
        WHERE (d.trashed_at IS NULL OR d.trashed_at > :as_of)
        {_stock_scope_sql(exclude_admin, "open_as_of" if open_as_of is not None else None)}
        {_entity_filter_clause(entities)}
        GROUP BY latest.to_stage
    """), _bind(as_of, open_as_of))).all()
    return {r[0]: r[1] for r in rows}


async def _sold_in_day(db: AsyncSession, day_start: datetime, day_end: datetime,
                       exclude_admin: bool = False, entities: list | None = None) -> int:
    """Tags that were in stock at the day's opening and are sold by its close —
    i.e. the day's sale records (Sale Date inside the day), which is what turns
    Opening into Closing."""
    not_stock = ", ".join(f"'{x}'" for x in _NOT_STOCK_STAGES)
    return (await db.execute(sa_text(f"""
        SELECT count(*)
        FROM ({_stock_reconstruction_sql(exclude_admin)}) latest
        JOIN devices d ON d.id = latest.device_id
        WHERE latest.to_stage = 'sold'
          AND latest.device_id IN (
              SELECT o.device_id
              FROM ({_stock_reconstruction_sql(exclude_admin, "open_as_of")}) o
              JOIN devices od ON od.id = o.device_id
              WHERE o.to_stage NOT IN ({not_stock})
                AND od.is_active AND NOT od.is_trashed
                AND (od.trashed_at IS NULL OR od.trashed_at > :open_as_of))
        {_entity_filter_clause(entities)}
    """), {"as_of": day_end, "open_as_of": day_start})).scalar() or 0


async def _stock_tags_as_of(db: AsyncSession, as_of: datetime, exclude_admin: bool = False,
                             entities: list | None = None, open_as_of: datetime | None = None) -> list:
    """Same reconstruction as _stock_as_of, but returns the actual tags instead
    of a count per stage — powers the Tag Based export, which lists every tag
    making up each number. Each row is (stage, barcode, category, make, model,
    cpu, ram, storage, grade, entity); Category follows the Ready to Sale page
    (device type, else sub-category)."""
    rows = (await db.execute(sa_text(f"""
        SELECT latest.to_stage, d.barcode,
               COALESCE(d.device_type, d.sub_category) AS category,
               d.brand, d.model, d.cpu,
               CASE WHEN d.ram_gb IS NOT NULL THEN d.ram_gb || 'GB' END AS ram,
               CASE WHEN d.storage_gb IS NOT NULL
                    THEN d.storage_gb || 'GB' || COALESCE(' ' || d.storage_type, '') END AS storage,
               d.grade::text AS grade, d.entity
        FROM ({_stock_reconstruction_sql(exclude_admin)}) latest
        JOIN devices d ON d.id = latest.device_id
        WHERE (d.trashed_at IS NULL OR d.trashed_at > :as_of)
        {_stock_scope_sql(exclude_admin, "open_as_of" if open_as_of is not None else None)}
        {_entity_filter_clause(entities)}
        ORDER BY latest.to_stage, d.barcode
    """), _bind(as_of, open_as_of))).all()
    return [tuple(r) for r in rows]


async def _stock_by_zone_as_of(db: AsyncSession, as_of: datetime, exclude_admin: bool = False,
                                entities: list | None = None, open_as_of: datetime | None = None) -> dict:
    """Count of devices by (stage, zone), reconstructed as of a point in
    time — powers the Location Export. Zone comes from the same latest-
    log-at-or-before-`as_of` pattern as stage (DISTINCT ON over
    device_location_logs, not a correlated subquery — device_location_logs
    has no per-device index, so a LATERAL join per device would be far
    slower than one sorted pass here). A device with no location log at all
    before `as_of` falls back to its *current* StorageLocation — same
    fallback routers/devices.py's _build_location_map uses for "current"
    lookups, just reached for a historical cutoff too since most devices
    are only ever placed once; 'Unassigned' if even that is empty."""
    rows = (await db.execute(sa_text(f"""
        WITH latest_loc AS (
            SELECT DISTINCT ON (dll.device_id) dll.device_id, sl.zone
            FROM device_location_logs dll
            JOIN storage_locations sl ON sl.id = dll.location_id
            WHERE dll.logged_at <= :as_of AND dll.location_id IS NOT NULL
            ORDER BY dll.device_id, dll.logged_at DESC
        )
        SELECT latest.to_stage, COALESCE(latest_loc.zone::text, cur_sl.zone::text, 'unassigned') AS zone, count(*)
        FROM ({_stock_reconstruction_sql(exclude_admin)}) latest
        JOIN devices d ON d.id = latest.device_id
        LEFT JOIN latest_loc ON latest_loc.device_id = d.id
        LEFT JOIN storage_locations cur_sl ON cur_sl.id = d.location_id
        WHERE (d.trashed_at IS NULL OR d.trashed_at > :as_of)
        {_stock_scope_sql(exclude_admin, "open_as_of" if open_as_of is not None else None)}
        {_entity_filter_clause(entities)}
        GROUP BY latest.to_stage, COALESCE(latest_loc.zone::text, cur_sl.zone::text, 'unassigned')
    """), _bind(as_of, open_as_of))).all()
    return {(r[0], r[1]): r[2] for r in rows}


def _parse_daily_stock_date(date: str) -> "datetime.date":
    try:
        return datetime.strptime(date.strip(), "%Y-%m-%d").date() if date.strip() else app_now().date()
    except ValueError:
        return app_now().date()


def _parse_multi(value: str) -> list:
    """Same convention as routers/devices.py's _multi(): the shared
    checkbox multiselect dropdown (_multiselect_filter.html) posts one
    comma-separated value per field rather than repeating the parameter."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


@router.get("/daily-stock", response_class=HTMLResponse)
async def daily_stock_report(
    request: Request,
    date: str = Query(default=""),
    exclude_admin: bool = Query(default=False),
    entity: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Opening Stock (reconstructed as of 00:00 the selected day) vs Closing
    Stock (as of 00:00 the next day) per stage — answers "how much inventory
    did we have at the start vs end of day X", which nothing else in this app
    shows (Stock Aging and the Dashboard pipeline are both current-moment-only
    snapshots, not a historical day boundary)."""
    day = _parse_daily_stock_date(date)
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)  # exclusive boundary == start of next day
    entities = _parse_multi(entity)

    opening_counts = await _stock_as_of(db, day_start, exclude_admin, entities)
    closing_counts = await _stock_as_of(db, day_end, exclude_admin, entities, open_as_of=day_start)
    sold_today = await _sold_in_day(db, day_start, day_end, exclude_admin, entities)

    rows = []
    total_open = total_close = 0
    for s in DeviceStage:
        if s == DeviceStage.l2:
            continue  # legacy stage, see DROPDOWN_STAGES comment in models/device.py
        if s.value in _NOT_STOCK_STAGES:
            continue  # sold tags have left; GRN Receipt duplicates the IQC/Stock In record
        o = opening_counts.get(s.value, 0)
        c = closing_counts.get(s.value, 0)
        if o == 0 and c == 0:
            continue  # keeps the table to stages that actually had stock that day
        total_open += o
        total_close += c
        rows.append({
            "stage": s.value, "label": STAGE_LABELS.get(s, s.value),
            "opening": o, "closing": c, "net": c - o,
        })

    return templates.TemplateResponse("reports/daily_stock.html", {
        "request": request, "current_user": current_user,
        "selected_date": day.isoformat(),
        "exclude_admin": exclude_admin,
        "selected_entity": entity,
        "entity_options": master_options("entity"),
        "rows": rows,
        "total_open": total_open, "total_close": total_close, "total_net": total_close - total_open,
        "sold_today": sold_today,
        "daily_reports": list_reports(),
        "is_today": day == app_now().date(),
    })


async def build_overall_csv(db: AsyncSession, day, exclude_admin: bool = False,
                            entities: list | None = None) -> str:
    """The on-screen table as-is (Stage/Opening/Closing/Net), with the
    Report Date repeated on every row — same convention as every other
    export in this app (e.g. Block Tags), so the file is self-describing
    even once detached from the page it came from. Shared by the on-demand
    export below and the 9 PM Daily Report generator (services/daily_stock_reports)."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    opening_counts = await _stock_as_of(db, day_start, exclude_admin, entities)
    closing_counts = await _stock_as_of(db, day_end, exclude_admin, entities, open_as_of=day_start)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report Date", "Stage", "Opening Stock", "Closing Stock", "Net Change"])
    for s in DeviceStage:
        if s == DeviceStage.l2 or s.value in _NOT_STOCK_STAGES:
            continue
        o = opening_counts.get(s.value, 0)
        c = closing_counts.get(s.value, 0)
        if o == 0 and c == 0:
            continue
        writer.writerow([day.isoformat(), STAGE_LABELS.get(s, s.value), o, c, c - o])
    return output.getvalue()


async def build_tags_csv(db: AsyncSession, day, exclude_admin: bool = False,
                         entities: list | None = None) -> str:
    """Same Opening/Closing reconstruction as the page, but one row per tag
    instead of a count per stage — the actual Tag Numbers behind each
    number in the Overall export."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    opening_tags = await _stock_tags_as_of(db, day_start, exclude_admin, entities)
    closing_tags = await _stock_tags_as_of(db, day_end, exclude_admin, entities, open_as_of=day_start)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report Date", "Snapshot", "Stage", "Tag Number",
                     "Category", "Make", "Model", "CPU", "RAM", "Storage", "Grade", "Entity"])
    for snapshot, tags in (("Opening", opening_tags), ("Closing", closing_tags)):
        for stage, barcode, *details in tags:
            writer.writerow([day.isoformat(), snapshot, STAGE_LABELS.get(DeviceStage(stage), stage), barcode,
                             *["" if v is None else v for v in details]])
    return output.getvalue()


async def build_location_csv(db: AsyncSession, day, exclude_admin: bool = False,
                             entities: list | None = None) -> str:
    """The on-screen table split out by Zone — one row per (Stage, Zone)
    that held any stock that day, instead of one row per Stage. See
    _stock_by_zone_as_of for how Zone is resolved at a historical cutoff."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    opening_counts = await _stock_by_zone_as_of(db, day_start, exclude_admin, entities)
    closing_counts = await _stock_by_zone_as_of(db, day_end, exclude_admin, entities, open_as_of=day_start)

    keys = sorted(set(opening_counts) | set(closing_counts))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report Date", "Stage", "Zone", "Opening Stock", "Closing Stock", "Net Change"])
    for stage, zone in keys:
        if stage == DeviceStage.l2.value or stage in _NOT_STOCK_STAGES:
            continue
        o = opening_counts.get((stage, zone), 0)
        c = closing_counts.get((stage, zone), 0)
        zone_label = "Unassigned" if zone == "unassigned" else ZONE_LABELS.get(ZoneType(zone), zone)
        stage_label = STAGE_LABELS.get(DeviceStage(stage), stage)
        writer.writerow([day.isoformat(), stage_label, zone_label, o, c, c - o])
    return output.getvalue()


def _csv_response(text: str, filename: str) -> StreamingResponse:
    return Response(
        content=text.encode(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/daily-stock/export/overall")
async def daily_stock_export_overall(
    date: str = Query(default=""),
    exclude_admin: bool = Query(default=False),
    entity: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    day = _parse_daily_stock_date(date)
    text = await build_overall_csv(db, day, exclude_admin, _parse_multi(entity))
    return _csv_response(text, f"daily_stock_overall_{day.isoformat()}.csv")


@router.get("/daily-stock/export/tags")
async def daily_stock_export_tags(
    date: str = Query(default=""),
    exclude_admin: bool = Query(default=False),
    entity: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    day = _parse_daily_stock_date(date)
    text = await build_tags_csv(db, day, exclude_admin, _parse_multi(entity))
    return _csv_response(text, f"daily_stock_tags_{day.isoformat()}.csv")


@router.get("/daily-stock/export/location")
async def daily_stock_export_location(
    date: str = Query(default=""),
    exclude_admin: bool = Query(default=False),
    entity: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    day = _parse_daily_stock_date(date)
    text = await build_location_csv(db, day, exclude_admin, _parse_multi(entity))
    return _csv_response(text, f"daily_stock_location_{day.isoformat()}.csv")


# ── Daily Report workbook (2026-10-21) ─────────────────────────────────────
# The three Daily Stock exports (Overall / Tag Based / Location Based) as
# three sheets of one .xlsx. Built from the same CSV text the separate
# exports produce, so the sheets can never disagree with those files.
_DS_SHEETS = (("overall", "Overall"), ("tags", "Tag Based"), ("location", "Location Based"))
_DS_NUMERIC = {"Opening Stock", "Closing Stock", "Net Change"}


def _daily_report_xlsx(csv_by_kind: dict) -> bytes:
    from datetime import date as _date
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="212529")
    for kind, title in _DS_SHEETS:
        ws = wb.create_sheet(title)
        rows = list(csv.reader(io.StringIO((csv_by_kind.get(kind) or "").lstrip("\ufeff"))))
        if not rows:
            continue
        header = rows[0]
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        date_col = header.index("Report Date") if "Report Date" in header else None
        num_cols = {i for i, h in enumerate(header) if h in _DS_NUMERIC}
        widths = [len(h) for h in header]
        for r in rows[1:]:
            out = []
            for i, v in enumerate(r):
                if i == date_col and v:
                    try:
                        v = _date.fromisoformat(v)   # a real date cell, not text
                    except ValueError:
                        pass
                elif i in num_cols and v.lstrip("-").isdigit():
                    v = int(v)
                out.append(v)
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(v)))
            ws.append(out)
            if date_col is not None:
                ws.cell(row=ws.max_row, column=date_col + 1).number_format = "yyyy-mm-dd"
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = min(max(w + 2, 10), 45)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/daily-stock/export/daily-report")
async def daily_stock_export_daily_report(
    date: str = Query(default=""),
    exclude_admin: bool = Query(default=False),
    entity: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Filter-area "Daily Report": all three exports for the chosen date /
    entity / admin filter, one sheet each."""
    day = _parse_daily_stock_date(date)
    ents = _parse_multi(entity)
    data = _daily_report_xlsx({
        "overall": await build_overall_csv(db, day, exclude_admin, ents),
        "tags": await build_tags_csv(db, day, exclude_admin, ents),
        "location": await build_location_csv(db, day, exclude_admin, ents),
    })
    return _xlsx_response(data, f"daily_report_{day.isoformat()}.xlsx")


@router.get("/daily-stock/report/{report_date}/workbook")
async def daily_stock_saved_workbook(
    report_date: str,
    current_user: User = Depends(get_current_user),
):
    """Daily Report table row: that day's three saved 9 PM files as one workbook."""
    from services.daily_stock_reports import report_file
    texts = {}
    for kind, _title in _DS_SHEETS:
        path = report_file(report_date, kind)
        if path is None:
            raise HTTPException(status_code=404, detail="Report not found")
        texts[kind] = path.read_text(encoding="utf-8")
    return _xlsx_response(_daily_report_xlsx(texts), f"daily_report_{report_date}.xlsx")


@router.get("/daily-stock/report/{report_date}/{kind}")
async def daily_stock_saved_report(
    report_date: str,
    kind: str,
    current_user: User = Depends(get_current_user),
):
    """Download one file of the "Daily Report" table (generated daily at 9 PM)."""
    from fastapi.responses import FileResponse
    from services.daily_stock_reports import report_file
    path = report_file(report_date, kind)
    if path is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, media_type="text/csv",
                        filename=f"daily_stock_{kind}_{report_date}.csv")


@router.get("/stage-movement", response_class=HTMLResponse)
async def stage_movement_report(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(StageMovement, Device.barcode, Device.brand, Device.model)
        .join(Device, StageMovement.device_id == Device.id)
        .order_by(StageMovement.moved_at.desc())
    )
    movements = result.all()
    return templates.TemplateResponse("reports/stage_movement.html", {
        "request": request, "movements": movements, "current_user": current_user
    })


# ── Orphaned "sold" devices: current_stage=sold but no Sale row exists.
# Legacy data from before 2026-09-21 (see models/device.py DROPDOWN_STAGES
# comment) — a bulk "Move to Stage" action (IQC/Devices Customise modal)
# could push a device straight to `sold` without going through the real
# Sale-creation flow, leaving it with no Sale row ever created. That fix only
# stops new orphans; the ones already stuck at current_stage=sold were
# otherwise invisible on both the Sales Report page and its CSV export
# forever. Shared by both below so they never drift apart (export showing
# rows the page doesn't, or vice versa). Dated by the StageMovement that
# actually moved the device to `sold` (Sale.sold_at doesn't exist for these),
# or included regardless of the date filter if even that movement record is
# missing. Shaped like the Sale-joined dict rows below so both callers can
# treat every row the same way from here on.
async def _sold_orphan_rows(db: AsyncSession, from_dt: datetime | None = None, to_dt: datetime | None = None) -> list[dict]:
    # Orphans first (usually a handful), using NOT EXISTS rather than NOT IN
    # over every sale; then the "moved to Sold" time is looked up for those
    # devices only — it used to aggregate every Sold stage movement in the
    # whole history on each page load, which made the Sales Report slow.
    orphan_result = await db.execute(
        select(Device.id, Device.barcode, Device.brand, Device.model, Device.grade,
               Device.sub_category, Lot.lot_number)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage == DeviceStage.sold,
               ~select(Sale.id).where(Sale.device_id == Device.id).exists())
    )
    orphans = orphan_result.all()
    orphan_ids = [o[0] for o in orphans]
    sold_moved_at = {}
    if orphan_ids:
        sold_moved_at = {
            r.device_id: r.moved_at
            for r in (await db.execute(
                select(StageMovement.device_id, func.max(StageMovement.moved_at).label("moved_at"))
                .where(StageMovement.to_stage == DeviceStage.sold,
                       StageMovement.device_id.in_(orphan_ids))
                .group_by(StageMovement.device_id)
            )).all()
        }
    rows = []
    for device_id, barcode, brand, model, grade, sub_category, lot_number in orphans:
        moved_at = sold_moved_at.get(device_id)
        if moved_at is not None and from_dt is not None and not (from_dt <= moved_at <= to_dt):
            continue
        rows.append({
            "sale_number": None, "sold_at": moved_at, "barcode": barcode,
            "brand": brand, "model": model, "lot_number": lot_number, "grade": grade,
            "sale_price": None, "customer_name": None, "customer_phone": None,
            "payment_mode": None, "sold_by": None, "has_sale": False,
            "device_id": device_id, "sub_category": sub_category,
            "invoice_no": None, "sales_person": None, "notes": None,
        })
    return rows


async def _latest_transfer_type_by_device(db: AsyncSession, device_ids: list) -> dict:
    """Most recent StockTransfer.transfer_type per device_id — the "Stage" the
    Sales Report export shows (e.g. "Ready For Sale" / "As Is Lot"), so sales
    ops can see which move flow last placed the device before it sold. Mirrors
    the latest-row-per-device pattern already used for location resolution
    elsewhere (e.g. routers/devices.py's _build_location_map)."""
    if not device_ids:
        return {}
    result = await db.execute(
        select(StockTransfer.device_id, StockTransfer.transfer_type, StockTransfer.created_at)
        .where(StockTransfer.device_id.in_(device_ids))
        .order_by(StockTransfer.created_at.desc())
    )
    latest: dict = {}
    for device_id, transfer_type, _created_at in result.all():
        if device_id not in latest:
            latest[device_id] = transfer_type
    return latest


@router.get("/sales", response_class=HTMLResponse)
async def sales_report(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Default: last 90 days; override with ?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD
    default_from = (datetime.now() - timedelta(days=90)).date()
    from_date_str = request.query_params.get("from_date", str(default_from))
    to_date_str   = request.query_params.get("to_date",   str(datetime.now().date()))

    # asyncpg requires datetime objects — parse strings before binding
    try:
        from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
        to_dt   = datetime.strptime(to_date_str,   "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        from_dt = datetime.now() - timedelta(days=90)
        to_dt   = datetime.now()
        from_date_str = from_dt.strftime("%Y-%m-%d")
        to_date_str   = to_dt.strftime("%Y-%m-%d")

    result = await db.execute(
        select(Sale, Device.barcode, Device.brand, Device.model, Device.grade, Lot.lot_number)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Sale.sold_at >= from_dt, Sale.sold_at <= to_dt)
        .order_by(Sale.sold_at.desc())
    )
    rows = [{
        "sale_number": s.Sale.sale_number, "sold_at": s.Sale.sold_at, "barcode": s.barcode,
        "brand": s.brand, "model": s.model, "lot_number": s.lot_number, "grade": s.grade,
        "sale_price": s.Sale.sale_price, "customer_name": s.Sale.customer_name,
        "customer_phone": s.Sale.customer_phone,
        "payment_mode": s.Sale.payment_mode, "sold_by": s.Sale.sold_by, "has_sale": True,
    } for s in result.all()]
    total = sum(float(r["sale_price"] or 0) for r in rows)

    rows.extend(await _sold_orphan_rows(db, from_dt, to_dt))
    rows.sort(key=lambda r: r["sold_at"] or datetime.min, reverse=True)

    # Rows go to the page as compact JSON and DataTables builds only the rows
    # on screen (deferRender) — rendering thousands of <tr>s made it slow.
    sales_data = [{
        "sn": r["sale_number"] or "", "has": bool(r["has_sale"]),
        "d": r["sold_at"].strftime("%d-%m-%Y") if r["sold_at"] else "",
        "ds": r["sold_at"].strftime("%Y%m%d%H%M%S") if r["sold_at"] else "",
        "tag": r["barcode"] or "",
        "dev": f"{r['brand'] or ''} {r['model'] or ''}".strip(),
        "lot": r["lot_number"] or "", "g": r["grade"] or "",
        "p": float(r["sale_price"]) if r["sale_price"] is not None else None,
        "c": r["customer_name"] or "", "pm": r["payment_mode"] or "", "by": r["sold_by"] or "",
    } for r in rows]
    return templates.TemplateResponse("reports/sales_report.html", {
        "request": request, "sales": rows, "sales_data": sales_data, "total": total, "current_user": current_user,
        "from_date": from_date_str, "to_date": to_date_str,
    })


async def _lot_pl_csv(db: AsyncSession, lot_ids=None) -> Response:
    """Lot P&L CSV. lot_ids: only these lots (the rows ticked on the page);
    None = every lot."""
    # Trashed lots excluded, same as the page — so "Export (N)" matches its rows.
    q = select(Lot).where(Lot.is_trashed.isnot(True)).order_by(Lot.created_at.desc()).limit(MAX_EXPORT_ROWS)
    if lot_ids is not None:
        q = q.where(Lot.id.in_(lot_ids))
    lots_result = await db.execute(q)
    lots = lots_result.scalars().all()

    rev_rows = await db.execute(
        select(Device.lot_id, func.coalesce(func.sum(Sale.sale_price), 0).label("revenue"))
        .join(Sale, Sale.device_id == Device.id).group_by(Device.lot_id)
    )
    rev_by_lot = {str(r.lot_id): float(r.revenue) for r in rev_rows}

    parts_rows = await db.execute(
        select(SparePartConsumption.lot_id, func.coalesce(func.sum(SparePartConsumption.total_cost), 0).label("parts"))
        .where(SparePartConsumption.lot_id.isnot(None)).group_by(SparePartConsumption.lot_id)
    )
    parts_by_lot = {str(r.lot_id): float(r.parts) for r in parts_rows}

    labour_rows = await db.execute(
        select(Device.lot_id, func.coalesce(func.sum(RepairAttempt.cost), 0).label("labour"))
        .join(RepairAttempt, RepairAttempt.device_id == Device.id).group_by(Device.lot_id)
    )
    labour_by_lot = {str(r.lot_id): float(r.labour) for r in labour_rows}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Lot#", "Supplier", "Date", "Qty", "Buying Price", "Parts Cost", "Labour Cost", "Total Cost", "Revenue", "Profit", "Margin%"])
    for lot in lots:
        lid = str(lot.id)
        revenue = rev_by_lot.get(lid, 0.0)
        parts   = parts_by_lot.get(lid, 0.0)
        labour  = labour_by_lot.get(lid, 0.0)
        buying  = float(lot.buying_price or 0)
        total   = buying + parts + labour
        profit  = revenue - total
        margin  = round(profit / revenue * 100, 1) if revenue > 0 else 0
        writer.writerow([lot.lot_number, lot.supplier_name, _csv_date(lot.purchase_date),
                         lot.qty, buying, parts, labour, total, revenue, profit, margin])
    if len(lots) == MAX_EXPORT_ROWS:
        writer.writerow(["# TRUNCATED", f"Export capped at {MAX_EXPORT_ROWS} rows", "", "", "", "", "", "", "", "", ""])
    output.seek(0)
    return Response(content=output.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lot_pl.csv"})


@router.get("/export/lot-pl")
async def export_lot_pl(db: AsyncSession = Depends(get_db), current_user: User = Depends(_require_financials)):
    return await _lot_pl_csv(db)


@router.post("/export/lot-pl")
async def export_lot_pl_selected(
    ids: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_require_financials),
    _csrf=Depends(verify_csrf),
):
    """The lots ticked on the Lot P&L page (2026-10-21). Empty = every lot."""
    import uuid as _uuid
    lot_ids = []
    for part in ids.split(","):
        try:
            lot_ids.append(_uuid.UUID(part.strip()))
        except ValueError:
            continue
    return await _lot_pl_csv(db, lot_ids or None)


# ── Tag-level exports (2026-10-16) ─────────────────────────────────────────
# Business P&L month row / Lot P&L lot row -> every tag behind that figure.
_TAG_EXPORT_HEADER = [
    "Entity", "Tag Number", "Lot Number", "Make Model", "CPU", "Gen", "RAM",
    "Storage", "Grade", "Location ID", "Zone", "Stage", "Device Price", "Sale Price",
]


def _zone_label(zone) -> str:
    if not zone:
        return ""
    if zone in ZONE_LABELS:
        return ZONE_LABELS[zone]
    try:
        return ZONE_LABELS.get(ZoneType(zone), str(zone))
    except ValueError:
        return str(zone)


async def _tag_export_csv(db: AsyncSession, rows, filename: str) -> StreamingResponse:
    """rows: (Device, lot_number, sale_price) tuples. Location ID / Zone use the
    same source as All Inventory: latest DeviceLocationLog entry, falling back
    to the device's own location_id, then its legacy warehouse / floor text."""
    from routers.devices import _build_location_map
    from models.location import StorageLocation
    ids = [d.id for d, _l, _p in rows]
    loc = {}
    for i in range(0, len(ids), 20000):
        loc.update(await _build_location_map(db, ids[i:i + 20000]))
    missing = [d.location_id for d, _l, _p in rows if str(d.id) not in loc and d.location_id]
    fallback = {}
    for i in range(0, len(missing), 20000):
        for sid, unit_id, zone in (await db.execute(
            select(StorageLocation.id, StorageLocation.unit_id, StorageLocation.zone)
            .where(StorageLocation.id.in_(missing[i:i + 20000]))
        )).all():
            fallback[sid] = {"unit_id": unit_id, "zone": zone}

    def _fmt_money(v):
        return "" if v is None else float(v)

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(_TAG_EXPORT_HEADER)
    for d, lot_number, sale_price in rows:
        li = loc.get(str(d.id)) or fallback.get(d.location_id) or {}
        if d.ram_gb:
            ram = f"{d.ram_gb}GB" + (f" {d.ram_type}" if d.ram_type else "")
        else:
            ram = d.total_ram_size or ""
        storage = (f"{d.storage_gb}GB" + (f" {d.storage_type}" if d.storage_type else "")) if d.storage_gb else (d.total_hdd_size or "")
        w.writerow([
            d.entity or "", d.barcode, lot_number or "",
            " ".join(x for x in (d.brand, d.model) if x),
            d.cpu or "", d.generation or "", ram, storage,
            getattr(d.grade, "value", d.grade) or "",
            li.get("unit_id") or d.warehouse or "",
            _zone_label(li.get("zone") or d.floor),
            STAGE_LABELS.get(d.current_stage, getattr(d.current_stage, "value", d.current_stage)),
            _fmt_money(d.device_price), _fmt_money(sale_price),
        ])
    return Response(
        content=out.getvalue().encode("utf-8-sig"), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/business-pl/export-month")
async def business_pl_export_month(
    year: int = Query(...), month: int = Query(..., ge=1, le=12),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_require_financials),
):
    """Every tag sold in this month — the same sales the Business P&L row's
    Revenue (sale price) and Device Cost (device price) columns add up."""
    rows = (await db.execute(
        select(Device, Lot.lot_number, Sale.sale_price)
        .join(Sale, Sale.device_id == Device.id)
        .outerjoin(Lot, Lot.id == Device.lot_id)
        .where(extract("year", Sale.sold_at) == year, extract("month", Sale.sold_at) == month)
        .order_by(Sale.sold_at, Device.barcode)
    )).all()
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return await _tag_export_csv(db, rows, f"business_pl_{names[month - 1]}_{year}_tags.csv")


@router.get("/business-pl/export-fy")
async def business_pl_export_fy(
    fy: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_require_financials),
):
    """Every tag sold in the whole Financial Year (Apr fy – Mar fy+1) — the
    twelve month exports in one file, same columns (2026-10-21)."""
    rows = (await db.execute(
        select(Device, Lot.lot_number, Sale.sale_price)
        .join(Sale, Sale.device_id == Device.id)
        .outerjoin(Lot, Lot.id == Device.lot_id)
        .where(Sale.sold_at >= datetime(fy, 4, 1), Sale.sold_at < datetime(fy + 1, 4, 1))
        .order_by(Sale.sold_at, Device.barcode)
    )).all()
    return await _tag_export_csv(db, rows, f"business_pl_FY_{fy}-{str(fy + 1)[-2:]}_tags.csv")


@router.get("/lot-pl/{lot_id}/export")
async def lot_pl_export_lot(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_require_financials),
):
    """Every live (non-trashed) tag of this lot, with its latest sale price
    when sold — the devices the Lot P&L row counts."""
    import uuid as _uuid
    try:
        lid = _uuid.UUID(lot_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Lot not found")
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    devices = (await db.execute(
        select(Device).where(Device.lot_id == lid, Device.is_trashed.isnot(True))
        .order_by(Device.barcode)
    )).scalars().all()
    latest_sale = {}
    ids = [d.id for d in devices]
    for i in range(0, len(ids), 20000):
        for did, price in (await db.execute(
            select(Sale.device_id, Sale.sale_price)
            .where(Sale.device_id.in_(ids[i:i + 20000]))
            .order_by(Sale.sold_at.desc())
        )).all():
            latest_sale.setdefault(did, price)
    rows = [(d, lot.lot_number, latest_sale.get(d.id)) for d in devices]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (lot.lot_number or "lot"))
    return await _tag_export_csv(db, rows, f"lot_pl_{safe}_tags.csv")


def _parse_range(from_date: str, to_date: str):
    """Sales Report date range -> (from_dt, to_dt), same parsing as the page;
    (None, None) when not given or unparseable (= all time)."""
    try:
        return (datetime.strptime(from_date, "%Y-%m-%d"),
                datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
    except (TypeError, ValueError):
        return None, None


@router.get("/export/sales")
async def export_sales(from_date: str = Query(default=""), to_date: str = Query(default=""),
                       db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Sales Report CSV. With from_date/to_date (what the page's Export button
    sends, 2026-10-21): exactly the page's rows; without: all time."""
    return await _sales_csv(db, *_parse_range(from_date, to_date))


@router.post("/export/sales")
async def export_sales_selected(from_date: str = Form(default=""), to_date: str = Form(default=""),
                                tags: str = Form(default=""),
                                db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(get_current_user),
                                _csrf=Depends(verify_csrf)):
    """The rows ticked on the Sales Report (Tags P&L) page, by tag number."""
    wanted = {t.strip().upper() for t in tags.split(",") if t.strip()} or None
    return await _sales_csv(db, *_parse_range(from_date, to_date), tags=wanted)


async def _sales_csv(db: AsyncSession, from_dt=None, to_dt=None, tags=None) -> Response:
    # Capped at the SQL level, same as before — the orphan set added below is
    # always small in practice (legacy stragglers, not an ongoing stream), so
    # bounding just the real-Sale query here is enough to keep this endpoint
    # from ever pulling an unbounded result set into memory.
    q = (
        select(Sale, Device.id, Device.barcode, Device.brand, Device.model,
               Device.sub_category, Lot.lot_number)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
    )
    if from_dt is not None:
        q = q.where(Sale.sold_at >= from_dt, Sale.sold_at <= to_dt)
    if tags:
        q = q.where(func.upper(Device.barcode).in_(list(tags)))
    result = await db.execute(q.order_by(Sale.sold_at.desc()).limit(MAX_EXPORT_ROWS))
    rows = [{
        "sale_number": s.Sale.sale_number, "sold_at": s.Sale.sold_at, "barcode": s.barcode,
        "brand": s.brand, "model": s.model, "lot_number": s.lot_number,
        "sale_price": s.Sale.sale_price, "customer_name": s.Sale.customer_name,
        "customer_phone": s.Sale.customer_phone, "payment_mode": s.Sale.payment_mode,
        "sold_by": s.Sale.sold_by,
        "device_id": s.id, "sub_category": s.sub_category,
        "invoice_no": s.Sale.invoice_no, "sales_person": s.Sale.sales_person,
        "notes": s.Sale.notes,
    } for s in result.all()]

    # Same "sold with no Sale row" devices the Sales Report page shows (see
    # _sold_orphan_rows above) — no date filter here, matching this export's
    # own existing all-time scope, so the CSV isn't a subset of the page.
    orphans = await _sold_orphan_rows(db, from_dt, to_dt)
    if tags:
        orphans = [r for r in orphans if (r["barcode"] or "").upper() in tags]
    rows.extend(orphans)
    rows.sort(key=lambda r: r["sold_at"] or datetime.min, reverse=True)
    truncated = len(rows) > MAX_EXPORT_ROWS
    rows = rows[:MAX_EXPORT_ROWS]

    stage_by_device = await _latest_transfer_type_by_device(db, [r["device_id"] for r in rows if r["device_id"]])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Sale#", "Invoice Number", "Date", "Barcode", "Brand", "Model", "Lot", "Price", "Customer", "Phone", "Payment", "Sold By",
                      "Sales Person", "Category", "Stage", "Remarks"])
    for r in rows:
        transfer_type = stage_by_device.get(r["device_id"])
        writer.writerow([
            r["sale_number"] or "NO SALE RECORD",
            r["invoice_no"] or "",
            _csv_date(r["sold_at"]),
            r["barcode"], r["brand"], r["model"], r["lot_number"],
            float(r["sale_price"]) if r["sale_price"] is not None else "",
            r["customer_name"] or "", r["customer_phone"] or "", r["payment_mode"] or "", r["sold_by"] or "",
            r["sales_person"] or "", r["sub_category"] or "",
            (transfer_type or "").replace("_", " ").title(), r["notes"] or "",
        ])
    if truncated:
        writer.writerow(["# TRUNCATED", f"Export capped at {MAX_EXPORT_ROWS} rows", "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
    output.seek(0)
    return Response(
        content=output.getvalue().encode(), media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=sales.csv",
            # No explicit cache headers previously — a browser could serve a
            # stale cached copy of this exact URL (no query params change
            # between downloads) from before a column was added here.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


async def _pl_calendar_year(db: AsyncSession, year: int) -> dict:
    """One calendar year of Business P&L figures, Jan..Dec (12-element
    lists), admin overrides applied — the building block the Financial Year
    view stitches Apr–Mar together from."""
    # Revenue — same shared computation as the Dashboard's Total Revenue
    # (admin Revenue override already applied).
    rev = await compute_year_revenue(db, year)

    # Device Cost — same shared computation as the Dashboard's Total Investment.
    device = await compute_year_device_cogs(db, year)
    # Parts / Labour — shared with the Dashboard's Parts Spent / Labour Spent.
    parts, labour = await compute_year_parts_labour_cogs(db, year)

    overridden = [False] * 12
    for o in (await db.execute(
        select(BusinessPLOverride).where(BusinessPLOverride.year == year)
    )).scalars().all():
        i = o.month - 1
        if not 0 <= i < 12:
            continue
        if any(v is not None for v in (o.revenue_override, o.device_cogs_override,
                                        o.parts_cogs_override, o.labour_cogs_override)):
            overridden[i] = True
    return {"rev": rev, "device": device, "parts": parts, "labour": labour, "overridden": overridden}


def _current_fy() -> int:
    t = app_now()
    return t.year if t.month >= 4 else t.year - 1


@router.get("/business-pl", response_class=HTMLResponse)
async def business_pl(
    request: Request,
    # Financial Year (Apr–Mar) by its starting year: 2025 = "2025-26"
    # (2026-10-15; was a calendar year Jan–Dec).
    fy: int = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_require_financials),
):
    if not fy:
        fy = _current_fy()

    # Apr..Dec of fy, then Jan..Mar of fy+1.
    y1 = await _pl_calendar_year(db, fy)
    y2 = await _pl_calendar_year(db, fy + 1)
    def _fy(key):
        return y1[key][3:] + y2[key][:3]
    monthly_rev          = _fy("rev")
    monthly_device_cogs  = _fy("device")
    monthly_parts_cogs   = _fy("parts")
    monthly_labour_cogs  = _fy("labour")
    monthly_overridden   = _fy("overridden")
    _names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    # Each row's real calendar year + month — what the Edit (override) form saves against.
    month_rows = ([{"label": f"{_names[m - 1]} {fy}", "year": fy, "month": m} for m in range(4, 13)]
                  + [{"label": f"{_names[m - 1]} {fy + 1}", "year": fy + 1, "month": m} for m in range(1, 4)])

    monthly_cogs = [d + p + l for d, p, l in
                    zip(monthly_device_cogs, monthly_parts_cogs, monthly_labour_cogs)]

    # ── FY totals — SUMS of the (possibly overridden) monthly rows, so the KPI
    # cards always agree with the Monthly Breakdown table. ────────────────────
    fy_from = datetime(fy, 4, 1)
    fy_to = datetime(fy + 1, 4, 1)
    total_revenue = sum(monthly_rev)
    total_sales_ct = (await db.execute(
        select(func.count(Sale.id))
        .where(Sale.sold_at >= fy_from, Sale.sold_at < fy_to)
    )).scalar() or 0
    inv_value = float((await db.execute(
        select(func.coalesce(func.sum(Device.device_price), 0))
        .where(Device.current_stage.notin_(["sold", "scrapped"]))
    )).scalar() or 0)

    total_cogs        = sum(monthly_cogs)
    total_device_cogs = sum(monthly_device_cogs)
    total_parts_cogs  = sum(monthly_parts_cogs)
    total_labour_cogs = sum(monthly_labour_cogs)

    # ── Lots Sold: count of lots where every active device is sold, scrapped,
    # or replaced — i.e. no device in the lot is still in-process. ───────────
    from sqlalchemy import exists, or_, not_
    _unresolved = (
        select(Device.id)
        .where(
            Device.lot_id == Lot.id, Device.is_active == True,
            not_(or_(
                Device.current_stage == DeviceStage.sold,
                Device.current_stage == DeviceStage.scrapped,
                Device.replaced.isnot(None),
            )),
        )
    )
    lots_sold_count = (await db.execute(
        select(func.count()).select_from(Lot)
        .where(
            exists(select(Device.id).where(Device.lot_id == Lot.id, Device.is_active == True)),
            not_(exists(_unresolved)),
        )
    )).scalar() or 0

    gross_profit      = total_revenue - total_cogs
    gross_margin      = round(gross_profit / total_revenue * 100, 1) if total_revenue > 0 else 0

    # Financial Year tabs: one per configured report year (2025 -> "2025-26"),
    # plus the current FY and the one being viewed, oldest first.
    from services.business_pl import fy_label
    _years = {int(v) for v in await report_year_values(db) if str(v).isdigit()}
    fy_choices = [(y, fy_label(y)) for y in sorted(_years | {_current_fy(), fy})]

    return templates.TemplateResponse("reports/business_pl.html", {
        "request": request, "current_user": current_user,
        "fy": fy, "fy_text": fy_label(fy), "fy_choices": fy_choices,
        "month_rows":           month_rows,
        "monthly_rev":          monthly_rev,
        "monthly_device_cogs":  monthly_device_cogs,
        "monthly_parts_cogs":   monthly_parts_cogs,
        "monthly_labour_cogs":  monthly_labour_cogs,
        "monthly_cogs":         monthly_cogs,
        "monthly_profit":       [r - c for r, c in zip(monthly_rev, monthly_cogs)],
        "monthly_overridden":   monthly_overridden,
        "total_revenue":        total_revenue,
        "total_cogs":           total_cogs,
        "total_device_cogs":    total_device_cogs,
        "total_parts_cogs":     total_parts_cogs,
        "total_labour_cogs":    total_labour_cogs,
        "lots_sold_count":      lots_sold_count,
        "gross_profit":         gross_profit,
        "gross_margin":         gross_margin,
        "total_sales_ct":       total_sales_ct,
        "avg_sale_price":       round(total_revenue / total_sales_ct, 0) if total_sales_ct else 0,
        "inv_value":            inv_value,
    })


@router.post("/business-pl/override")
async def save_business_pl_override(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    revenue: str = Form(""),
    device_cogs: str = Form(""),
    parts_cogs: str = Form(""),
    labour_cogs: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admin-only override of one month's Business P&L row. Each field is
    independent: leave it blank to clear that field's override (fall back to
    the computed value); fill it in to pin that field to a specific number."""
    if current_user.role.value != "admin":
        return RedirectResponse(
            url=f"/reports/business-pl?fy={year if month >= 4 else year - 1}&error=Admin+access+required",
            status_code=302)

    def _parse(v: str):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return Decimal(v)
        except InvalidOperation:
            return None

    row = (await db.execute(
        select(BusinessPLOverride).where(
            BusinessPLOverride.year == year, BusinessPLOverride.month == month)
    )).scalar_one_or_none()
    if not row:
        row = BusinessPLOverride(year=year, month=month)
        db.add(row)

    old = {
        "revenue": row.revenue_override, "device_cogs": row.device_cogs_override,
        "parts_cogs": row.parts_cogs_override, "labour_cogs": row.labour_cogs_override,
    }
    row.revenue_override     = _parse(revenue)
    row.device_cogs_override = _parse(device_cogs)
    row.parts_cogs_override  = _parse(parts_cogs)
    row.labour_cogs_override = _parse(labour_cogs)
    row.updated_by = current_user.username

    await audit(db, user=current_user, action="BUSINESS_PL_OVERRIDE_SAVED",
                table_name="business_pl_overrides", record_id=f"{year}-{month:02d}",
                old_value={k: str(v) if v is not None else None for k, v in old.items()},
                new_value={
                    "revenue": str(row.revenue_override) if row.revenue_override is not None else None,
                    "device_cogs": str(row.device_cogs_override) if row.device_cogs_override is not None else None,
                    "parts_cogs": str(row.parts_cogs_override) if row.parts_cogs_override is not None else None,
                    "labour_cogs": str(row.labour_cogs_override) if row.labour_cogs_override is not None else None,
                },
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/reports/business-pl?fy={year if month >= 4 else year - 1}&success=Row+updated",
        status_code=302)


@router.get("/stock-aging", response_class=HTMLResponse)
async def stock_aging_report(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    EXCLUDED_VALUES = ["sold", "scrapped", "returned"]
    result = await db.execute(
        select(Device)
        .where(Device.current_stage.notin_(EXCLUDED_VALUES))
        .order_by(Device.current_stage, Device.created_at)
    )
    devices = result.scalars().all()

    now = app_now()
    BRACKETS = [
        ("0–7 days",   0,   7),
        ("8–30 days",  8,  30),
        ("31–60 days", 31, 60),
        ("61–90 days", 61, 90),
        ("90+ days",   91, 9999),
    ]
    bracket_labels = [b[0] for b in BRACKETS]

    stage_data     = defaultdict(lambda: {b[0]: {"count": 0, "cost": 0.0} for b in BRACKETS})
    stage_totals   = defaultdict(lambda: {"count": 0, "cost": 0.0})
    bracket_totals = {b[0]: {"count": 0, "cost": 0.0} for b in BRACKETS}
    grand          = {"count": 0, "cost": 0.0}
    aged_list      = []

    for dev in devices:
        # A device with no created_at (seen on older/imported rows) counts as
        # added today instead of crashing the whole report (2026-10-17).
        age   = (now - (dev.created_at or now)).days
        cost  = float(dev.device_price or 0)
        blabel = BRACKETS[-1][0]
        for label, lo, hi in BRACKETS:
            if lo <= age <= hi:
                blabel = label
                break

        try:
            slabel = STAGE_LABELS.get(DeviceStage(dev.current_stage), str(dev.current_stage))
        except ValueError:
            slabel = str(dev.current_stage)

        stage_data[slabel][blabel]["count"]    += 1
        stage_data[slabel][blabel]["cost"]     += cost
        stage_totals[slabel]["count"]          += 1
        stage_totals[slabel]["cost"]           += cost
        bracket_totals[blabel]["count"]        += 1
        bracket_totals[blabel]["cost"]         += cost
        grand["count"] += 1
        grand["cost"]  += cost
        aged_list.append({
            "barcode": dev.barcode,
            "brand":   dev.brand or "—",
            "model":   dev.model or "—",
            "stage":   slabel,
            "age":     age,
            "cost":    cost,
            "grade":   dev.grade or "—",
        })

    aged_list.sort(key=lambda x: x["age"], reverse=True)

    return templates.TemplateResponse("reports/stock_aging.html", {
        "request":        request,
        "current_user":   current_user,
        "stage_data":     dict(stage_data),
        "stage_totals":   dict(stage_totals),
        "bracket_totals": bracket_totals,
        "brackets":       bracket_labels,
        "grand":          grand,
        "oldest_devices": aged_list[:50],
    })


@router.get("/receivables", response_class=HTMLResponse)
async def receivables_report(
    request: Request,
    export: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_require_receivables),
):
    """Dealer receivables ageing — outstanding orders bucketed by days overdue."""
    from decimal import Decimal
    from models.dealers import Dealer, DealerOrder

    OUTSTANDING = ("pending", "confirmed", "delivered")
    now = app_now()

    rows_result = await db.execute(
        select(DealerOrder, Dealer)
        .join(Dealer, DealerOrder.dealer_id == Dealer.id)
        .where(DealerOrder.due_amount > 0, DealerOrder.status.in_(OUTSTANDING))
        .order_by(Dealer.business_name, DealerOrder.order_date)
    )
    rows = rows_result.all()

    def _bucket(order):
        if not order.payment_due_date:
            return "current"
        days = (now - order.payment_due_date).days
        if days <= 0:
            return "current"
        if days <= 30:
            return "d30"
        if days <= 60:
            return "d60"
        if days <= 90:
            return "d90"
        return "d90plus"

    ageing = []
    totals = {k: Decimal("0") for k in ("current", "d30", "d60", "d90", "d90plus")}
    for order, dealer in rows:
        b = _bucket(order)
        due = order.due_amount or Decimal("0")
        ageing.append({"order": order, "dealer": dealer, "bucket": b, "due": due})
        totals[b] += due
    totals["grand"] = sum(totals.values())

    if export == "csv":
        def _gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Dealer", "Order #", "Order Date", "Due Date", "Bucket", "Due Amount"])
            for r in ageing:
                dealer_name = r["dealer"].business_name or f"{r['dealer'].first_name or ''} {r['dealer'].last_name or ''}".strip()
                w.writerow([
                    dealer_name,
                    r["order"].order_number,
                    r["order"].order_date.strftime("%d-%m-%Y"),
                    r["order"].payment_due_date.strftime("%d-%m-%Y") if r["order"].payment_due_date else "",
                    r["bucket"],
                    float(r["due"]),
                ])
            yield buf.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            _gen(),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f"attachment; filename=receivables_{now.strftime('%Y%m%d')}.csv"},
        )

    return templates.TemplateResponse("reports/receivables.html", {
        "request": request,
        "current_user": current_user,
        "ageing": ageing,
        "totals": totals,
        "as_of": now,
    })


@router.get("/overdue", response_class=HTMLResponse)
async def overdue_report(
    request: Request,
    stage: str = Query(default=""),
    min_days: int = Query(default=3, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = app_now()
    cutoff = now - timedelta(days=min_days)

    filters = [
        Device.current_stage.notin_([DeviceStage.sold, DeviceStage.scrapped]),
        Device.updated_at <= cutoff,
    ]
    if stage:
        try:
            filters.append(Device.current_stage == DeviceStage(stage))
        except ValueError:
            pass

    stmt = (
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where(*filters)
        .order_by(Device.updated_at.asc())
    )

    result = await db.execute(stmt)
    rows = []
    for device, lot_number in result.all():
        days = (now - device.updated_at).days if device.updated_at else 0
        rows.append({
            "barcode":    device.barcode,
            "brand":      device.brand or "",
            "model":      device.model or "",
            "stage":      device.current_stage.value if device.current_stage else "",
            "days":       days,
            "lot_number": lot_number or "",
            "updated_at": device.updated_at,
        })

    return templates.TemplateResponse("reports/overdue.html", {
        "request": request,
        "rows": rows,
        "stage": stage,
        "min_days": min_days,
        "all_stages": [s for s in DROPDOWN_STAGES if s not in (DeviceStage.sold, DeviceStage.scrapped)],
        "stage_labels": STAGE_LABELS,
        "current_user": current_user,
    })


@router.get("/overdue/csv")
async def overdue_csv(
    stage: str = Query(default=""),
    min_days: int = Query(default=3, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = app_now()
    cutoff = now - timedelta(days=min_days)

    filters = [
        Device.current_stage.notin_([DeviceStage.sold, DeviceStage.scrapped]),
        Device.updated_at <= cutoff,
    ]
    if stage:
        try:
            filters.append(Device.current_stage == DeviceStage(stage))
        except ValueError:
            pass

    stmt = (
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where(*filters)
        .order_by(Device.updated_at.asc())
    )

    result = await db.execute(stmt.limit(MAX_EXPORT_ROWS))
    rows_all = result.all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Barcode", "Brand", "Model", "Stage", "Days in Stage", "Lot Number", "Last Updated"])
    for device, lot_number in rows_all:
        days = (now - device.updated_at).days if device.updated_at else 0
        writer.writerow([
            device.barcode,
            device.brand or "",
            device.model or "",
            device.current_stage.value if device.current_stage else "",
            days,
            lot_number or "",
            device.updated_at.strftime("%Y-%m-%d") if device.updated_at else "",
        ])
    if len(rows_all) == MAX_EXPORT_ROWS:
        writer.writerow(["# TRUNCATED", f"Export capped at {MAX_EXPORT_ROWS} rows", "", "", "", "", ""])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=overdue_devices.csv"},
    )
