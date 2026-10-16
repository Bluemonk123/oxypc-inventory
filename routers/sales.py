"""
Sales Router — sale block enforcement + return re-entry to IQC + audit
"""
from templates_config import templates
from urllib.parse import quote_plus
import uuid as _uuid
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from utils.timezone import app_now, app_today
from decimal import Decimal
from fastapi import APIRouter, Depends, Form, File, Request, HTTPException, Query, BackgroundTasks, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
import csv
import io
from sqlalchemy import select, func, text, or_, update, case
from fastapi.responses import StreamingResponse

from database import get_db
from utils.csv_decode import decode_csv_bytes
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, STAGE_LABELS
from models.lot import Lot
from models.location import StorageLocation
from models.as_is_lot_price import AsIsLotPrice
from models.sales import Sale, Return, CN_STAGES, CN_STAGE_ACTION_LABELS
from models.company import Company
from models.crm import CRMSalesOpportunity, CRMContact
from models.dispatch_request import TelecallerDispatchRequest
from models.block_tags import BlockRecord, BlockRecordItem, BlockItemStatus, SOLD_NOTE_PREFIX
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm
from services.control_engine import validate_sale_allowed
from services.cost_engine import (
    check_below_cost_warning, get_or_create_costings, below_cost_warning_for,
)
from services.audit_engine import audit
from services.parts_changed import parts_badge_map, parts_display_map, spec_cell
from services.ready_pricing import (
    load_ready_rates, unit_price, tag_min_price, lot_price, as_is_min_price,
)
from services.event_bus import EventType, publish
from utils.touch_type import touch_badge_html
from utils.flag import flag_badge_html
from services.location_defaults import ensure_stage_location
from utils.warranty import (
    warranty_from_sold_at, latest_sold_at_map,
    parse_warranty_duration, warranty_status_for_sale,
)


# Model Summary "Total Quantity" grade split: (stored grade, label, badge class).
# Colours follow the Grade column of the Ready for Sale Tags table. Scrap is
# left out by request (2026-10-13): such tags still count in the Total.
_SUMMARY_GRADES = (
    ("A", "A", "bg-success"),
    ("B", "B", "bg-warning text-dark"),
    ("C", "C", "bg-danger"),
    ("D", "D", "bg-danger"),
    ("E", "E", "bg-info text-dark"),
    ("", "NG", "bg-light text-dark border"),   # NG = no grade set
)

router = APIRouter(tags=["sales"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.sales, UserRole.sales_manager, UserRole.telecaller)
# Opened to every role (2026-09-03) — was require_roles(admin, sales,
# sales_manager, telecaller), same as `allowed` above. Reused by the page
# view itself, Multi-Request, Multi-Sell, and Upload Tags alike, so any
# narrower gate on just one of those (e.g. Upload Tags) would be a no-op —
# a user outside the old role set couldn't reach the page to use it anyway.
ready_allowed = get_current_user


async def ensure_sale_number_seq(db: AsyncSession) -> None:
    """Create sale_number_seq if this database never got it (2026-10-17).

    It is made by a one-off script (migrate_fix_prod_sale_seq.py), not by the
    schema auto-provisioner, so a database that missed that script 500'd on
    /sales/new. Checked with to_regclass (no failing statement, so no rollback
    of the caller's transaction), and seeded past the highest existing
    SALE-#### so a recreated sequence can't re-issue a number in use.
    Same idea as routers/part_sales.py's part_sale_number_seq self-heal."""
    exists = (await db.execute(text("SELECT to_regclass('sale_number_seq')"))).scalar()
    if exists:
        return
    highest = (await db.execute(text(
        "SELECT COALESCE(MAX(NULLIF(regexp_replace(sale_number, '\\D', '', 'g'), '')::bigint), 0) FROM sales"
    ))).scalar() or 0
    await db.execute(text(
        "CREATE SEQUENCE IF NOT EXISTS sale_number_seq START WITH %d" % (int(highest) + 1)))


async def _next_sale_number(db: AsyncSession) -> str:
    await ensure_sale_number_seq(db)
    result = await db.execute(text("SELECT nextval('sale_number_seq')"))
    seq = result.scalar()
    return f"SALE-{seq:04d}"


def _entity_values(raw: str) -> list:
    """Entity multi-select posts one comma-separated value (see
    templates/_multiselect_filter.html); entity names never contain a comma."""
    return [v.strip() for v in (raw or "").split(",") if v.strip()]



def _touch_clause(touch: str):
    """Touch Type filter for the Ready to Sale tables: "touch" = only tags Final
    QC marked Touch; "non_touch" = everything else (blank shows as NT)."""
    t = (touch or "").strip().lower()
    if t == "touch":
        return Device.touch_type == "touch"
    if t == "non_touch":
        return or_(Device.touch_type.is_(None), Device.touch_type != "touch")
    return None


def _device_type_of(device) -> str:
    """Device Type as these tables show it: device_type, else sub_category
    (blank on many records, sub_category means the same thing)."""
    return (device.device_type or "").strip() or (device.sub_category or "").strip()


_device_type_expr = func.coalesce(func.nullif(func.trim(Device.device_type), ""), Device.sub_category)


def _row_passes(device, touch: str, entities: list, dtypes: list | None = None) -> bool:
    """Same Touch Type + Entity filter as _touch_clause, applied in Python to the
    already-loaded rows behind the Model Summary / As-Is Lot tables."""
    t = (touch or "").strip().lower()
    is_touch = (device.touch_type or "").strip().lower() == "touch"
    if t == "touch" and not is_touch:
        return False
    if t == "non_touch" and is_touch:
        return False
    if entities and (device.entity or "") not in entities:
        return False
    if dtypes and _device_type_of(device) not in dtypes:
        return False
    return True

def _grade_passes(device, grades: set) -> bool:
    """Model Summary Grades filter: empty = every grade; "NG" = no grade set."""
    if not grades:
        return True
    g = getattr(device.grade, "value", device.grade) if device.grade else ""
    return (g or "NG") in grades


@router.get("/sales/ready/barcodes")
async def ready_list_barcodes(
    entity: str = "",
    touch: str = "",
    device_type: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(ready_allowed),
):
    """Every ready-to-sale barcode, for "Select All" on the Ready to Sale table.

    With client-side DataTables every row existed in the DOM regardless of
    page, so ticking Select All selected every ready-to-sale device, not just
    the one page on screen. Server-side paging renders only the current page,
    so that behaviour needs the full barcode list from here instead — kept
    deliberately cheap (barcodes only) rather than reusing /data, which builds
    full row HTML per device.
    """
    entities = _entity_values(entity)
    barcodes = (await db.execute(
        select(Device.barcode).where(
            Device.current_stage == DeviceStage.ready_to_sale,
            Device.is_trashed == False,
            or_(Device.sub_lot_number.is_(None), Device.sub_lot_number == ""),
            *([Device.entity.in_(entities)] if entities else []),
            *([_touch_clause(touch)] if _touch_clause(touch) is not None else []),
            *([_device_type_expr.in_(_entity_values(device_type))] if _entity_values(device_type) else []),
        )
    )).scalars().all()
    return {"barcodes": [b for b in barcodes if b]}


@router.get("/sales/ready/data")
async def ready_list_data(
    request: Request,
    draw: int = 1, start: int = 0, length: int = 25,
    entity: str = "",
    touch: str = "",
    device_type: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(ready_allowed),
):
    """DataTables server-side feed for Ready to Sale.

    At ~4,500 ready-to-sale devices this table rendered every row into the
    HTML on every load. Rows now come a page at a time.

    Multi-Sell/Multi-Request select across ALL pages, not just the one on
    screen — a bulk tag upload can tick devices that server-side paging never
    renders. That selection is tracked client-side in a Set of barcodes rather
    than by reading checkbox state from the DOM, so it survives every page
    change; see readySelected in the template's script block.
    """
    from sqlalchemy import desc as _desc, asc as _asc
    from html import escape
    from models.role_permissions import can_view_pricing as _cvp

    role = getattr(current_user.role, "value", current_user.role)
    show_pricing = _cvp(role)
    from models.role_permissions import can_set_price as _csp
    show_set_price = _csp(role)
    rates = await load_ready_rates(db)

    # As-Is sub-lot devices (non-empty sub_lot_number) are locked to the As-Is
    # Lot table until released via "Open Tag" — excluded here so they never
    # also show in the Tag Table while still grouped.
    not_as_is = or_(Device.sub_lot_number.is_(None), Device.sub_lot_number == "")
    # Entity filter (multi-select above the table): applies to the rows, the
    # totals and the paging alike, so the table and its counts always agree.
    entities = _entity_values(entity)
    entity_filter = [Device.entity.in_(entities)] if entities else []
    if _touch_clause(touch) is not None:
        entity_filter.append(_touch_clause(touch))
    if _entity_values(device_type):
        entity_filter.append(_device_type_expr.in_(_entity_values(device_type)))

    base = (
        select(Device, Lot.lot_number, Lot.buying_price, Lot.qty, Lot.selling_price)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed == False, not_as_is,
               *entity_filter)
    )
    count_q = select(func.count()).select_from(Device).where(
        Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed == False, not_as_is,
        *entity_filter)
    total = (await db.execute(count_q)).scalar() or 0

    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = []
    if search:
        like = f"%{search}%"
        search_filters.append(or_(
            Device.barcode.ilike(like), Device.brand.ilike(like), Device.model.ilike(like),
        ))
    # Without a search term this is the same query as the total above, and
    # DataTables asks on every draw — only pay for it when a term narrows the set.
    if search_filters:
        filtered_q = (
            select(func.count()).select_from(Device).join(Lot, Device.lot_id == Lot.id)
            .where(Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed == False,
                   not_as_is, *entity_filter, *search_filters)
        )
        filtered = (await db.execute(filtered_q)).scalar() or 0
    else:
        filtered = total

    col_map = {1: Device.barcode, 2: Lot.lot_number, 4: Device.brand, 5: Device.model, 8: Device.grade}
    try:
        order_col = int(request.query_params.get("order[0][column]", 0))
    except ValueError:
        order_col = 0
    order_dir = request.query_params.get("order[0][dir]", "desc")
    sort_expr = col_map.get(order_col, Device.updated_at)
    order_by = _desc(sort_expr) if order_dir != "asc" else _asc(sort_expr)
    if order_col == 0:
        order_by = Device.updated_at.desc()

    rows = (await db.execute(
        base.where(*search_filters).order_by(order_by, Device.barcode)
        .offset(max(0, start)).limit(min(max(1, length), 5000))
    )).all()

    device_ids = [d.id for d, *_ in rows]
    approved_ids, requested_ids, rejected_notes_map = set(), set(), {}
    if device_ids:
        for did, st, notes in (await db.execute(
            select(TelecallerDispatchRequest.device_id, TelecallerDispatchRequest.status,
                   TelecallerDispatchRequest.rejected_notes)
            .where(TelecallerDispatchRequest.device_id.in_(device_ids))
        )).all():
            if st == "approved":
                approved_ids.add(str(did))
            elif st == "requested":
                requested_ids.add(str(did))
            elif st == "rejected":
                rejected_notes_map[str(did)] = notes or ""
    sold_map = await latest_sold_at_map(db, device_ids)
    warranty_map = {}
    for did, sold_at in sold_map.items():
        w = warranty_from_sold_at(sold_at)
        if w:
            warranty_map[did] = w

    # Location ID — reads the device's LATEST DeviceLocationLog entry first,
    # same as All Inventory / Device Detail / Inventory Manager (see
    # routers/devices.py's _build_location_map docstring) — Device.location_id
    # alone is only a fallback for a device with no log entry at all. Most
    # devices are assigned via the pickup-placeback flow, which only ever
    # writes the log, so reading Device.location_id directly left this column
    # blank for almost every real device.
    from routers.devices import _build_location_map
    log_map = await _build_location_map(db, device_ids)
    location_map = {did: v["unit_id"] for did, v in log_map.items() if v.get("unit_id")}
    missing_loc_ids = [d.id for d, *_ in rows if str(d.id) not in location_map and d.location_id]
    if missing_loc_ids:
        for did, unit_id in (await db.execute(
            select(Device.id, StorageLocation.unit_id)
            .join(StorageLocation, Device.location_id == StorageLocation.id)
            .where(Device.id.in_(missing_loc_ids))
        )).all():
            location_map[str(did)] = unit_id

    assigned_ids = {d.assigned_to_user_id for d, *_ in rows if d.assigned_to_user_id}
    assigned_name_map = {}
    if assigned_ids:
        assigned_name_map = {
            u.id: (u.full_name or u.username)
            for u in (await db.execute(select(User).where(User.id.in_(assigned_ids)))).scalars().all()
        }

    parts_badges, parts_data = await parts_display_map(db, device_ids)

    def esc(v):
        return escape(str(v)) if v is not None else ""

    data = []
    for d, lot_number, buying_price, lot_qty, selling_price in rows:
        did = str(d.id)
        unit = unit_price(d, buying_price, lot_qty)
        if unit is None:
            unit_cost, cost_source = 0.0, "none"
        else:
            unit_cost, cost_source = float(unit), ("device" if d.device_price else "lot")
        # Min Selling Price = unit price + Other Rates + margin for each item
        # (services/ready_pricing.py). A price saved via "Set price" is an
        # explicit override and always wins over the formula.
        auto_min = tag_min_price(unit, rates)
        gv = getattr(d.grade, "value", d.grade) if d.grade else ""
        gcls = ("success" if gv == "A" else "warning text-dark" if gv == "B" else
                "danger" if gv in ("C", "D", "scrap") else "secondary")
        approved, requested = did in approved_ids, did in requested_ids
        rejected_notes = rejected_notes_map.get(did)
        w = warranty_map.get(did)

        loc_name = location_map.get(did)
        assigned_name = assigned_name_map.get(d.assigned_to_user_id)
        cells = [
            (f'<input type="checkbox" class="form-check-input readyChk" value="{esc(d.barcode)}" '
             f'data-serial="{esc(d.serial_no or "")}">'),
            (f'<span class="text-nowrap"><a href="/devices/{esc(d.barcode)}" class="text-decoration-none"><code class="fw-bold">{esc(d.barcode)}</code></a>'
             + touch_badge_html(d.touch_type) + flag_badge_html(d)
             + parts_badges.get(did, '') + '</span>'
             + (f'<br><span class="badge bg-info text-dark">{esc(d.entity)}</span>' if d.entity else '')),
            (f'<a href="/devices?lot={esc(lot_number)}" class="btn btn-sm py-0 px-2 small text-decoration-none" '
             f'style="background-color:#ffffff;border:1px solid #6C757D;color:#6C757D;">{esc(lot_number)}</a>'),
            f'<span class="font-monospace">{esc(loc_name)}</span>' if loc_name else '<span class="text-muted">—</span>',
            esc(d.brand or "—"), esc(d.model or "—"),
            spec_cell(parts_data, d.id, "RAM", f"{d.ram_gb}GB" if d.ram_gb else "—"),
            spec_cell(parts_data, d.id, "Hard Drive", f"{d.storage_gb}GB" if d.storage_gb else "—"),
            f'<span class="badge bg-{gcls}">{esc(gv) or "—"}</span>',
        ]
        if show_pricing:
            if cost_source == "none":
                cells.append('<span class="text-muted">—</span>')
            else:
                avg = ' <span class="text-muted ms-1" style="font-size:.7rem">avg</span>' if cost_source == "lot" else ""
                cells.append(f'<span class="fw-semibold">₹{unit_cost:,.0f}{avg}</span>')
        cells.append(
            f'<span class="badge bg-{"success" if w["status"] == "active" else "secondary"}">{esc(w["label"])}</span>'
            if w else '<span class="text-muted">—</span>'
        )
        # Open/Blocked badge reflects Device.is_blocked directly — Blocked
        # while a Block Tags hold is open (see models/block_tags.py), Open
        # otherwise. The Name above it is Device.assigned_to_user_id, which
        # Block Tags' New Block Tags / Edit modals set to the Block For user
        # on top of however this page's own Assign action already sets it.
        assign_badge = ('<span class="badge bg-danger">Blocked</span>' if d.is_blocked
                         else '<span class="badge bg-success">Open</span>')
        cells.append(
            (esc(assigned_name) if assigned_name else '<span class="text-muted">—</span>')
            + f'<br>{assign_badge}'
        )
        if d.min_selling_price is not None:
            cells.append(f'₹{float(d.min_selling_price):,.0f}')
        elif auto_min is not None:
            cells.append(
                f'₹{float(auto_min):,.0f}'
                '<span class="text-muted ms-1" style="font-size:.7rem" '
                'title="Calculated: unit price + Other Rates + margin. Use Set price to override.">auto</span>')
        else:
            cells.append('<span class="text-muted">—</span>')
        cells.append(f'₹{float(d.max_selling_price):,.0f}' if d.max_selling_price is not None
                    else '<span class="text-muted">—</span>')
        action = f'<a href="/sales/new?barcodes={esc(d.barcode)}&qty=1" class="btn btn-sm btn-success">Sell</a>'
        if approved:
            action += '<span class="badge bg-success align-self-center ms-1">Approved</span>'
        elif requested:
            action += '<span class="badge bg-warning text-dark align-self-center ms-1">Requested</span>'
        else:
            if rejected_notes is not None:
                action += f'<span class="badge bg-danger align-self-center ms-1" title="{esc(rejected_notes)}">Rejected</span>'
            action += (f'<button class="btn btn-sm btn-outline-primary ms-1 d-none" data-bs-toggle="modal" data-bs-target="#dispatchModal" '
                      f'data-barcode="{esc(d.barcode)}" data-model="{esc((d.brand or "") + " " + (d.model or ""))}">Request</button>')
        if show_set_price:  # Master Data → Pricing Visibility → Set Price
            action += (f'<button type="button" class="btn btn-sm btn-outline-dark ms-1 tag-set-price-btn" '
                      f'data-barcode="{esc(d.barcode)}" '
                      f'data-min="{d.min_selling_price if d.min_selling_price is not None else ""}" '
                      f'data-auto="{auto_min if auto_min is not None else ""}" '
                      f'data-max="{d.max_selling_price if d.max_selling_price is not None else ""}">Set price</button>')
        cells.append(f'<div class="d-flex gap-1 flex-nowrap text-nowrap">{action}</div>')
        data.append(cells)

    return {"draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data}


@router.get("/sales/ready", response_class=HTMLResponse)
async def ready_list(request: Request, db: AsyncSession = Depends(get_db),
                     current_user: User = Depends(ready_allowed)):
    # Per-tab filters (Touch Type + Entity) for the three server-rendered tabs:
    # ms_* = Ready for Sale Model Summary, al_* = As-Is Lot, am_* = As-Is
    # Model Summary. The Tag table filters through /sales/ready/data instead.
    qp = request.query_params
    ms_touch, ms_entities = qp.get("ms_touch", ""), _entity_values(qp.get("ms_entity", ""))
    al_touch, al_entities = qp.get("al_touch", ""), _entity_values(qp.get("al_entity", ""))
    am_touch, am_entities = qp.get("am_touch", ""), _entity_values(qp.get("am_entity", ""))
    # Grades filter on the two Model Summary tabs: A/B/C/D/E, "NG" = no grade.
    ms_grades = set(_entity_values(qp.get("ms_grade", "")))
    am_grades = set(_entity_values(qp.get("am_grade", "")))
    ms_dtypes = _entity_values(qp.get("ms_dtype", ""))
    al_dtypes = _entity_values(qp.get("al_dtype", ""))
    am_dtypes = _entity_values(qp.get("am_dtype", ""))
    result = await db.execute(
        select(Device, Lot.lot_number, Lot.buying_price, Lot.qty, Lot.selling_price)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed == False)
        .order_by(Device.updated_at.desc())
    )
    devices = result.all()

    # ── Model Summary for Ready to Sale: one row per (model, brand), aggregated
    # purchase/sale price using the same per-device-else-lot-average logic as
    # the main table's Unit Cost column. ──────────────────────────────────────
    # Two summaries (2026-10-10): "Ready for Sale Model Summary" covers only
    # the Ready for Sale tags (no sub-lot) and "As-Is Model Summary" only the
    # As-Is tags (sub-lot set), each shown in the tab next to its own table.
    # One row per configuration (Model + Make + CPU + Gen + RAM + Storage).
    #   Total Quantity    = tags of that configuration Ready for Sale now,
    #                       with the split by grade (A 3, B 2, ...) under it.
    #                       (The Sold count shown here before was removed by
    #                       request, 2026-10-13.)
    #   Total Purchase    = sum of unit prices (device price, else lot average)
    #   Min Selling Price = sum of the tags' own Min Selling Price (a saved
    #                       "Set price" wins, else the same formula as the
    #                       Tag table: unit price + Other Rates + margin)
    summary_rates = await load_ready_rates(db)

    def _cfg_key(model, brand, cpu, ram_gb, storage_gb, storage_type, generation=None):
        return ((model or "Unknown Model"), (brand or "Unknown Make"), (cpu or "").strip(),
                ram_gb or 0, storage_gb or 0, (storage_type or "").strip(),
                (generation or "").strip())

    def _build_model_summary(rows):
        summary: dict = {}
        for device, lot_number, buying_price, lot_qty, selling_price in rows:
            unit = unit_price(device, buying_price, lot_qty)
            unit_cost = float(unit) if unit is not None else 0.0
            if device.min_selling_price is not None:
                tag_min = float(device.min_selling_price)
            else:
                _auto = tag_min_price(unit, summary_rates)
                tag_min = float(_auto) if _auto is not None else None
            key = _cfg_key(device.model, device.brand, device.cpu, device.ram_gb,
                           device.storage_gb, device.storage_type, device.generation)
            g = summary.setdefault(key, {
                "key": key, "model": key[0], "make": key[1],
                "device_type": device.device_type or device.sub_category or "—",
                "cpu": key[2] or "—",
                "gen": key[6] or "—",
                "ram": f"{key[3]} GB" if key[3] else "—",
                "storage": (f"{key[4]} GB" + (f" {key[5]}" if key[5] else "")) if key[4] else "—",
                "total_count": 0, "total_purchase_price": 0.0,
                "min_total": 0.0, "min_units": [], "barcodes": [], "grade_counts": {},
            })
            g["total_count"] += 1
            grade = getattr(device.grade, "value", device.grade) if device.grade else ""
            g["grade_counts"][grade] = g["grade_counts"].get(grade, 0) + 1
            g["total_purchase_price"] += unit_cost
            if tag_min is not None:
                g["min_total"] += tag_min
                g["min_units"].append(tag_min)
            g["barcodes"].append(device.barcode)
        out = sorted(summary.values(), key=lambda g: g["total_count"], reverse=True)
        for g in out:
            g["min_lo"] = min(g["min_units"]) if g["min_units"] else None
            g["min_hi"] = max(g["min_units"]) if g["min_units"] else None
            g["min_missing"] = g["total_count"] - len(g["min_units"])
            # Grades in fixed order (A, B, C, D, E, then no grade), only
            # the ones this configuration has: [(label, count, badge class)].
            gc = g.pop("grade_counts")
            g["grades"] = [
                (label, gc[code], cls) for code, label, cls in _SUMMARY_GRADES if gc.get(code)
            ]
        return out

    model_summary_ready = _build_model_summary(
        [r for r in devices if not (r[0].sub_lot_number or "").strip()
         and _row_passes(r[0], ms_touch, ms_entities, ms_dtypes)
         and _grade_passes(r[0], ms_grades)])
    model_summary_as_is = _build_model_summary(
        [r for r in devices if (r[0].sub_lot_number or "").strip()
         and _row_passes(r[0], am_touch, am_entities, am_dtypes)
         and _grade_passes(r[0], am_grades)])


    # Dispatch-request state (approved/requested/rejected) and warranty used to
    # be computed here for every ready-to-sale device (~4,500 with no filter),
    # purely to feed the old inline table's rows. Both now live in
    # /sales/ready/data, built per page instead of for the whole set.
    device_ids = [d.id for d, *_ in devices]

    # ── Interested dealers banner: open CRM sales opps matching ready device types ──
    ready_device_types = {d.device_type for d, *_ in devices if d.device_type}
    interested_dealers: list = []
    if ready_device_types:
        opps = (await db.execute(
            select(CRMSalesOpportunity, CRMContact.company_name, CRMContact.phone)
            .outerjoin(CRMContact, CRMSalesOpportunity.contact_id == CRMContact.id)
            .where(
                CRMSalesOpportunity.device_type.in_(ready_device_types),
                CRMSalesOpportunity.stage.notin_(["won", "lost"]),
            )
            .order_by(CRMSalesOpportunity.priority.desc(), CRMSalesOpportunity.updated_at.desc())
            .limit(20)
        )).all()
        interested_dealers = [
            {
                "opp_number": opp.opp_number,
                "title": opp.title,
                "device_type": opp.device_type,
                "grade": opp.grade_required or "Any",
                "qty": opp.required_qty,
                "budget": opp.budget_per_unit,
                "stage": opp.stage,
                "priority": opp.priority,
                "company": company or "Unknown",
                "phone": phone or "",
            }
            for opp, company, phone in opps
        ]

    # ── Ready to Sale As-Is Lot: one row per (Lot, Sub-Lot) pair that
    # currently has ready-to-sale stock. Built from `devices` above (already
    # stage-filtered) for Availability/Device Type/Min-Max Price, plus one
    # extra query for Total Quantities — the same (lot_id, sub_lot_number)
    # pair's device count across EVERY stage, not just ready-to-sale. ───────
    from collections import Counter
    as_is_groups: dict = {}
    for device, lot_number, buying_price, lot_qty, _selling_price in devices:
        if not device.sub_lot_number:
            continue
        if not _row_passes(device, al_touch, al_entities, al_dtypes):
            continue
        key = (device.lot_id, device.sub_lot_number)
        g = as_is_groups.setdefault(key, {
            "lot_number": lot_number, "device_types": Counter(),
            "models": Counter(), "model_configs": Counter(),
            "cpus": set(), "rams": set(), "storages": set(),
            "availability": 0, "barcodes": [], "unit_prices": [], "max_prices": [],
            "assigned_to_user_id": device.assigned_to_user_id,
        })
        # device_type is blank on a lot of real records; sub_category ("Laptop"/
        # "Desktop"/"TFT"/...) is populated far more often and means the same
        # thing — same fallback templates/sales/detail.html already uses.
        g["device_types"][device.device_type or device.sub_category or "—"] += 1
        if device.model:
            g["models"][device.model] += 1
        # Model Names popup: one line per configuration + grade.
        _grade = getattr(device.grade, "value", device.grade) if device.grade else ""
        g["model_configs"][(
            device.model or "—", device.brand or "—",
            device.device_type or device.sub_category or "—",
            (device.cpu or "").strip() or "—", (device.generation or "").strip() or "—",
            f"{device.ram_gb} GB" if device.ram_gb else "—",
            (f"{device.storage_gb} GB" + (f" {device.storage_type}" if device.storage_type else ""))
            if device.storage_gb else "—",
            "NG" if not _grade else ("Scrap" if _grade == "scrap" else _grade),
        )] += 1
        if device.cpu:
            g["cpus"].add(device.cpu)
        if device.ram_gb:
            g["rams"].add(f"{device.ram_gb} GB")
        if device.storage_gb:
            g["storages"].add(f"{device.storage_gb} GB" + (f" {device.storage_type}" if device.storage_type else ""))
        g["availability"] += 1
        g["barcodes"].append(device.barcode)
        g["unit_prices"].append(unit_price(device, buying_price, lot_qty))
        if device.max_selling_price is not None:
            g["max_prices"].append(float(device.max_selling_price))
        # First device in the group wins — sub-lots are assigned in bulk
        # (GRN Post-IQC / Transfers), so every device normally agrees anyway.
        if g["assigned_to_user_id"] is None and device.assigned_to_user_id is not None:
            g["assigned_to_user_id"] = device.assigned_to_user_id

    as_is_lots = []
    if as_is_groups:
        lot_ids = list({lid for lid, _ in as_is_groups.keys()})
        total_rows = (await db.execute(
            select(Device.lot_id, Device.sub_lot_number, func.count(Device.id))
            .where(Device.lot_id.in_(lot_ids), Device.sub_lot_number.isnot(None),
                   Device.sub_lot_number != "", Device.is_active == True)  # noqa: E712
            .group_by(Device.lot_id, Device.sub_lot_number)
        )).all()
        total_map = {(lid, sl): c for lid, sl, c in total_rows}

        rates = await load_ready_rates(db)
        override_map = {
            (o.lot_id, o.sub_lot_number): o
            for o in (await db.execute(
                select(AsIsLotPrice).where(AsIsLotPrice.lot_id.in_(lot_ids))
            )).scalars().all()
        }

        assigned_ids = {g["assigned_to_user_id"] for g in as_is_groups.values() if g["assigned_to_user_id"]}
        user_name_map = {}
        if assigned_ids:
            user_name_map = {
                u.id: (u.full_name or u.username)
                for u in (await db.execute(select(User).where(User.id.in_(assigned_ids)))).scalars().all()
            }

        for (lot_id, sub_lot), g in as_is_groups.items():
            device_type_breakdown = [{"name": n, "count": c} for n, c in sorted(g["device_types"].items())]
            model_breakdown = [{"name": n, "count": c} for n, c in sorted(g["models"].items())]
            _grade_order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "Scrap": 5, "NG": 6}
            model_details = [
                {"model": k[0], "make": k[1], "device_type": k[2], "cpu": k[3], "gen": k[4],
                 "ram": k[5], "storage": k[6], "grade": k[7], "qty": c}
                for k, c in sorted(g["model_configs"].items(),
                                   key=lambda kc: (kc[0][0], kc[0][1], kc[0][2], kc[0][3], kc[0][4],
                                                   kc[0][5], kc[0][6], _grade_order.get(kc[0][7], 9)))
            ]
            # Lot Price = what the available tags are worth together (sum of
            # their unit prices); Min Selling Price = Lot Price + As-Is margin
            # unless "Set price" saved an explicit override for this lot.
            priced = [u for u in g["unit_prices"] if u is not None]
            lot_total = lot_price(priced) if priced else None
            auto_min = as_is_min_price(lot_total, rates) if lot_total is not None else None
            override = override_map.get((lot_id, sub_lot))
            override_min = override.min_selling_price if override else None
            override_max = override.max_selling_price if override else None
            as_is_lots.append({
                "lot_id": str(lot_id), "lot_number": g["lot_number"],
                "sub_lot_number": sub_lot,
                "device_type_breakdown": device_type_breakdown,
                "device_type_count": len(device_type_breakdown),
                "model_breakdown": model_breakdown,
                "model_details": model_details,
                "model_count": len(model_breakdown),
                "cpu": ", ".join(sorted(g["cpus"])),
                "ram": ", ".join(sorted(g["rams"])),
                "storage": ", ".join(sorted(g["storages"])),
                "total_qty": total_map.get((lot_id, sub_lot), g["availability"]),
                "availability": g["availability"],
                "lot_price": float(lot_total) if lot_total is not None else None,
                "min_price": float(override_min if override_min is not None else auto_min)
                             if (override_min is not None or auto_min is not None) else None,
                "min_is_auto": override_min is None,
                "min_override": str(override_min) if override_min is not None else "",
                "min_auto": str(auto_min) if auto_min is not None else "",
                "max_price": float(override_max) if override_max is not None
                             else (max(g["max_prices"]) if g["max_prices"] else None),
                "max_override": str(override_max) if override_max is not None else "",
                "barcodes": g["barcodes"],
                "assigned_to_name": user_name_map.get(g["assigned_to_user_id"], "—"),
                "assigned_to_user_id": str(g["assigned_to_user_id"]) if g["assigned_to_user_id"] else "",
            })
        as_is_lots.sort(key=lambda r: (r["lot_number"] or "", r["sub_lot_number"] or ""))

    from utils.master_data import entity_values
    return templates.TemplateResponse("sales/ready_list.html", {
        "entity_options": await entity_values(db),
        "request": request, "devices": devices, "current_user": current_user,
        "interested_dealers": interested_dealers,
        "model_summary_ready": model_summary_ready,
        "model_summary_as_is": model_summary_as_is,
        "f": {"ms_touch": ms_touch, "ms_entity": qp.get("ms_entity", ""),
              "al_touch": al_touch, "al_entity": qp.get("al_entity", ""),
              "am_touch": am_touch, "am_entity": qp.get("am_entity", ""),
              "ms_dtype": qp.get("ms_dtype", ""), "al_dtype": qp.get("al_dtype", ""),
              "am_dtype": qp.get("am_dtype", ""),
              "ms_grade": qp.get("ms_grade", ""), "am_grade": qp.get("am_grade", "")},
        "summary_grade_options": [(code or "NG", label if code else "NG (No grade)")
                                  for code, label, _cls in _SUMMARY_GRADES],
        # Device Type filter options: the types actually present in Ready to Sale.
        "device_type_options": sorted({_device_type_of(r[0]) for r in devices if _device_type_of(r[0])}),
        "ready_tag_count": sum(1 for r in devices if not (r[0].sub_lot_number or "").strip()),
        "as_is_tag_count": sum(1 for r in devices if (r[0].sub_lot_number or "").strip()),
        "as_is_lots": as_is_lots,
        # Assigned To choices for the admin-only As-Is Lot Edit modal.
        "as_is_assign_users": (await db.execute(
            select(User).where(User.status == True).order_by(User.full_name)  # noqa: E712
        )).scalars().all() if current_user.role == UserRole.admin else [],
    })


def _role_can_set_price(user) -> bool:
    """Master Data → Pricing Visibility → Set Price, enforced on the server too
    so a hidden button can't be bypassed with a direct request."""
    from models.role_permissions import can_set_price as _csp
    return _csp(getattr(user.role, "value", user.role))


@router.post("/sales/ready/as-is-lot/price")
async def set_as_is_lot_price(
    request: Request,
    lot_id: str = Form(...),
    sub_lot_number: str = Form(...),
    min_selling_price: str = Form(""),
    max_selling_price: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Ready to Sale As-Is Lot table's Set price modal — stores a LOT-level
    Min/Max Selling Price override for this (Lot, Sub-Lot) pair in
    as_is_lot_prices, replacing the table's computed Min Selling Price (Lot
    Price + As-Is margin). Both fields blank clears the override and the lot
    goes back to the formula. Deliberately not written to Device.min_selling_
    price: that column is per-tag, and a lot total there would become every
    tag's price once the lot is opened."""
    if not _role_can_set_price(current_user):
        return RedirectResponse(url="/sales/ready?error=Your+role+cannot+set+prices", status_code=302)
    try:
        lid = _uuid.UUID(lot_id)
    except ValueError:
        return RedirectResponse(url="/sales/ready?error=Invalid+lot", status_code=302)
    sub_lot = sub_lot_number.strip()
    if not sub_lot:
        return RedirectResponse(url="/sales/ready?error=Missing+Sub-Lot+Number", status_code=302)
    try:
        min_val = Decimal(min_selling_price) if min_selling_price.strip() else None
        max_val = Decimal(max_selling_price) if max_selling_price.strip() else None
    except Exception:
        return RedirectResponse(url="/sales/ready?error=Invalid+price", status_code=302)

    existing = (await db.execute(
        select(AsIsLotPrice).where(AsIsLotPrice.lot_id == lid,
                                   AsIsLotPrice.sub_lot_number == sub_lot)
    )).scalar_one_or_none()
    old_value = ({"min_selling_price": str(existing.min_selling_price)
                  if existing.min_selling_price is not None else None,
                  "max_selling_price": str(existing.max_selling_price)
                  if existing.max_selling_price is not None else None}
                 if existing else None)

    if min_val is None and max_val is None:
        if existing:
            await db.delete(existing)
    elif existing:
        existing.min_selling_price = min_val
        existing.max_selling_price = max_val
        existing.updated_by = current_user.username
    else:
        db.add(AsIsLotPrice(lot_id=lid, sub_lot_number=sub_lot,
                            min_selling_price=min_val, max_selling_price=max_val,
                            updated_by=current_user.username))

    await audit(db, user=current_user, action="AS_IS_LOT_PRICE_SET",
                # record_id is VARCHAR(50): the lot id alone fits; the Sub-Lot
                # Number lives in the logged values (lot id + ":" + Sub-Lot
                # overflowed for any Sub-Lot over 13 characters).
                table_name="as_is_lot_prices", record_id=str(lot_id),
                old_value=old_value,
                new_value={"sub_lot_number": sub_lot, "min_selling_price": str(min_val) if min_val is not None else None,
                           "max_selling_price": str(max_val) if max_val is not None else None},
                request=request)
    await db.commit()
    return RedirectResponse(url="/sales/ready?success=Selling+price+updated", status_code=302)


@router.post("/sales/ready/as-is-lot/edit")
async def edit_as_is_lot(
    request: Request,
    lot_id: str = Form(...),
    sub_lot_number: str = Form(...),
    new_sub_lot_number: str = Form(...),
    assigned_to_user_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin)),
):
    """Ready to Sale As-Is Lot table's Edit button (admin only): change the
    Sub-Lot Number and/or Assigned To of every tag in this (Lot, Sub-Lot) —
    the whole group regardless of stage, same scope as Open Tag, so the lot
    never ends up split. A saved lot price moves with a renamed sub-lot.
    Renaming onto a Sub-Lot Number the lot already has is refused (that would
    merge two lots)."""
    try:
        lid = _uuid.UUID(lot_id)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid lot"}, status_code=400)
    old_sub = sub_lot_number.strip()
    new_sub = re.sub(r"\s+", " ", new_sub_lot_number).strip()
    if not old_sub or not new_sub:
        return JSONResponse({"ok": False, "error": "Enter a Sub-Lot Number."}, status_code=400)
    if len(new_sub) > 50:
        return JSONResponse({"ok": False, "error": "Sub-Lot Number can be at most 50 characters."}, status_code=400)
    assign_uid = None
    if assigned_to_user_id.strip():
        try:
            assign_uid = _uuid.UUID(assigned_to_user_id.strip())
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid user"}, status_code=400)
        if not (await db.execute(select(User.id).where(User.id == assign_uid))).scalar_one_or_none():
            return JSONResponse({"ok": False, "error": "User not found"}, status_code=400)

    group = (Device.lot_id == lid, Device.sub_lot_number == old_sub, Device.is_trashed == False)  # noqa: E712
    before = (await db.execute(
        select(Device.assigned_to_user_id, func.count(Device.id)).where(*group)
        .group_by(Device.assigned_to_user_id)
    )).all()
    if not before:
        return JSONResponse({"ok": False, "error": "No tags found for this Sub-Lot"}, status_code=404)
    renamed = new_sub != old_sub
    if renamed:
        clash = (await db.execute(
            select(func.count(Device.id)).where(Device.lot_id == lid, Device.sub_lot_number == new_sub,
                                                Device.is_trashed == False)  # noqa: E712
        )).scalar()
        if clash:
            return JSONResponse({"ok": False, "error": f"This lot already has a Sub-Lot {new_sub}. "
                                 "Choose a different Sub-Lot Number."}, status_code=400)

    # Any database error is reported in the popup (and logged) instead of a
    # bare 500, so a failure on one deployment can be diagnosed from the UI.
    try:
        result = await db.execute(
            update(Device).where(*group)
            .values(sub_lot_number=new_sub, assigned_to_user_id=assign_uid)
        )
        if renamed:
            price = (await db.execute(
                select(AsIsLotPrice).where(AsIsLotPrice.lot_id == lid, AsIsLotPrice.sub_lot_number == old_sub)
            )).scalar_one_or_none()
            if price:
                stale_target = (await db.execute(
                    select(AsIsLotPrice).where(AsIsLotPrice.lot_id == lid, AsIsLotPrice.sub_lot_number == new_sub)
                )).scalar_one_or_none()
                if stale_target:  # left over from a lot opened earlier under that number
                    await db.delete(stale_target)
                    await db.flush()
                price.sub_lot_number = new_sub

        await audit(db, user=current_user, action="AS_IS_LOT_EDITED",
                    table_name="devices", record_id=str(lot_id),  # VARCHAR(50); Sub-Lot in values
                    old_value={"sub_lot_number": old_sub,
                               "assigned_to_user_id": {str(u) if u else None: c for u, c in before}},
                    new_value={"sub_lot_number": new_sub,
                               "assigned_to_user_id": str(assign_uid) if assign_uid else None,
                               "tags_updated": result.rowcount},
                    request=request)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        import logging
        logging.getLogger(__name__).exception("As-Is lot edit failed for %s / %s", lot_id, old_sub)
        msg = str(getattr(exc, "orig", exc)).strip().splitlines()[0][:300] if str(exc) else exc.__class__.__name__
        return JSONResponse({"ok": False, "error": f"Could not save: {msg}"}, status_code=500)
    return JSONResponse({"ok": True, "tags_updated": result.rowcount, "sub_lot_number": new_sub})


@router.post("/sales/ready/as-is-lot/open")
async def open_as_is_lot(
    request: Request,
    lot_id: str = Form(...),
    sub_lot_number: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Ready to Sale As-Is Lot table's Open Tag action — releases every device
    sharing this (Lot, Sub-Lot) pair back into the plain Tag Table by clearing
    Device.sub_lot_number. Cleared for the WHOLE sub-lot group regardless of
    current_stage (not just ready_to_sale), same scope as
    set_as_is_lot_price's bulk write, so nothing is left half-released."""
    try:
        lid = _uuid.UUID(lot_id)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid lot"}, status_code=400)
    sub_lot = sub_lot_number.strip()
    if not sub_lot:
        return JSONResponse({"ok": False, "error": "Missing Sub-Lot Number"}, status_code=400)

    result = await db.execute(
        update(Device).where(Device.lot_id == lid, Device.sub_lot_number == sub_lot,
                              Device.is_trashed == False)
        .values(sub_lot_number=None)
    )
    if not result.rowcount:
        return JSONResponse({"ok": False, "error": "No tags found for this Sub-Lot"}, status_code=404)

    # The lot no longer exists as a lot, so its price override goes with it —
    # otherwise a later sub-lot reusing this number would inherit a stale price.
    stale = (await db.execute(
        select(AsIsLotPrice).where(AsIsLotPrice.lot_id == lid,
                                   AsIsLotPrice.sub_lot_number == sub_lot)
    )).scalar_one_or_none()
    if stale:
        await db.delete(stale)

    await audit(db, user=current_user, action="AS_IS_LOT_OPENED",
                table_name="devices", record_id=str(lot_id),  # VARCHAR(50); Sub-Lot in values
                old_value={"sub_lot_number": sub_lot},
                new_value={"sub_lot_number": None, "tags_released": result.rowcount},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "tags_released": result.rowcount})


@router.post("/sales/ready/set-price")
async def set_ready_tag_price(
    request: Request,
    barcode: str = Form(...),
    min_selling_price: str = Form(""),
    max_selling_price: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Ready to Sale Tag table's Set price modal — writes Min/Max Selling
    Price on a single Tag Number."""
    if not _role_can_set_price(current_user):
        return JSONResponse({"ok": False, "error": "Your role cannot set prices"}, status_code=403)
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        return JSONResponse({"ok": False, "error": "Tag Number not found"}, status_code=404)
    try:
        min_val = Decimal(min_selling_price) if min_selling_price.strip() else None
        max_val = Decimal(max_selling_price) if max_selling_price.strip() else None
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid price"}, status_code=400)

    old_value = {
        "min_selling_price": str(device.min_selling_price) if device.min_selling_price is not None else None,
        "max_selling_price": str(device.max_selling_price) if device.max_selling_price is not None else None,
    }
    device.min_selling_price = min_val
    device.max_selling_price = max_val
    await audit(db, user=current_user, action="TAG_PRICE_SET",
                table_name="devices", record_id=str(device.id),
                old_value=old_value,
                new_value={"min_selling_price": str(min_val) if min_val is not None else None,
                           "max_selling_price": str(max_val) if max_val is not None else None},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/sales/ready/bulk-set-price")
async def bulk_set_ready_tag_price(
    request: Request,
    barcodes: str = Form(...),
    min_selling_price: str = Form(""),
    max_selling_price: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Ready to Sale Tag table's Bulk Set price modal — writes Min/Max Selling
    Price across every selected Tag Number at once, same one-write-across-many
    shape as the As-Is Lot Edit modal, just keyed by an explicit barcode list
    instead of (Lot, Sub-Lot)."""
    if not _role_can_set_price(current_user):
        return JSONResponse({"ok": False, "error": "Your role cannot set prices"}, status_code=403)
    bcs = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not bcs:
        return JSONResponse({"ok": False, "error": "No tags selected"}, status_code=400)
    try:
        min_val = Decimal(min_selling_price) if min_selling_price.strip() else None
        max_val = Decimal(max_selling_price) if max_selling_price.strip() else None
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid price"}, status_code=400)

    result = await db.execute(
        update(Device).where(Device.barcode.in_(bcs))
        .values(min_selling_price=min_val, max_selling_price=max_val)
    )
    await audit(db, user=current_user, action="TAG_PRICE_BULK_SET",
                table_name="devices", record_id=f"bulk:{len(bcs)}_tags",
                new_value={"min_selling_price": str(min_val) if min_val is not None else None,
                           "max_selling_price": str(max_val) if max_val is not None else None,
                           "tags_updated": result.rowcount},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "updated": result.rowcount})


@router.post("/sales/ready/upload-tags")
async def upload_ready_tags(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(ready_allowed),
):
    """Bulk Upload Tags on Ready to Sale — reads a single-column CSV of tag
    numbers and reports back which are ready to sale, so the page can tick the
    matching rows. Read-only: selects nothing, changes nothing."""
    content = await file.read()
    text_data = decode_csv_bytes(content)
    reader = csv.DictReader(io.StringIO(text_data))

    # Accept `tag_number`/`barcode`, any casing, plus the same "Tag No" /
    # "Tag Number" aliases the IQC bulk upload already accepts
    # (routers/bulk_upload.py _TAG_HEADER_ALIASES) — this app's own CSV
    # exports elsewhere (Transfers, WorkID Status) use "Tag Number" as the
    # header, and re-uploading one of those here used to fail with "CSV must
    # have a 'tag_number' column header" even though the column was right
    # there, just spelled with a space instead of an underscore.
    field_map = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    key = (field_map.get("tag_number") or field_map.get("barcode")
           or field_map.get("tag number") or field_map.get("tag no"))
    if not key:
        return JSONResponse(
            {"error": "CSV must have a 'tag_number' (or 'barcode'/'Tag No'/'Tag Number') column header"},
            status_code=400,
        )

    tags, errors = [], []
    seen = set()
    for i, row in enumerate(reader, start=2):
        tag = (row.get(key) or "").strip()
        if not tag:
            errors.append(f"Row {i}: tag_number is empty")
            continue
        if tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)

    if not tags:
        return JSONResponse({"found": [], "not_found": [], "not_ready": [], "errors": errors})

    # Matched case-insensitively — same fix as /devices' own bulk tag upload
    # (routers/devices.py): tag numbers get typed into spreadsheets by hand
    # and come back lowercase, while the stored barcode is often upper (or
    # vice versa — this codebase's barcodes are inconsistently cased). An
    # exact IN() reported every one of those as "not found" against a tag
    # that plainly exists and is sitting in Ready to Sale.
    rows = (await db.execute(
        select(Device.barcode, Device.current_stage).where(
            func.upper(Device.barcode).in_([t.upper() for t in tags])
        )
    )).all()
    by_upper = {}
    for stored_barcode, stage in rows:
        by_upper.setdefault((stored_barcode or "").upper(), []).append((stored_barcode, stage))

    found, not_found, not_ready = [], [], []
    for tag in tags:
        matches = by_upper.get(tag.upper())
        if not matches:
            not_found.append(tag)
            continue
        # Echo the barcode as STORED, not as typed — the page ticks rows by
        # exact barcode string, so echoing the user's casing back would
        # select nothing.
        for stored_barcode, stage in matches:
            if stage != DeviceStage.ready_to_sale:
                not_ready.append(stored_barcode)
            else:
                found.append(stored_barcode)

    return JSONResponse({"found": found, "not_found": not_found,
                         "not_ready": not_ready, "errors": errors})


def _unit_stock_price(device, lot) -> float:
    """Device unit cost — device-level price if set, else avg lot cost (same
    logic as the Ready-to-Sale Unit Cost column)."""
    if device is not None and device.device_price:
        return float(device.device_price)
    if lot is not None and lot.qty:
        return float(lot.buying_price or 0) / lot.qty
    return 0.0


async def block_id_by_sale_number(db: AsyncSession, sale_numbers) -> dict:
    """{sale_number: Block ID} for sales recorded through a block's Sell
    button. Selling closes each sold BlockRecordItem with notes =
    SOLD_NOTE_PREFIX + sale number (see create_sale), which is the link."""
    notes = [SOLD_NOTE_PREFIX + n for n in sale_numbers if n]
    if not notes:
        return {}
    rows = (await db.execute(
        select(BlockRecordItem.notes, BlockRecord.block_id)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(BlockRecordItem.notes.in_(notes))
    )).all()
    return {n[len(SOLD_NOTE_PREFIX):]: b for n, b in rows}


async def _block_sale_context(db: AsyncSession, block_record_id: str):
    """(BlockRecord, {device_id: still-blocked BlockRecordItem}) for a block
    being sold from Block Tags, or (None, {}) when the id is blank/unknown."""
    try:
        rid = _uuid.UUID((block_record_id or "").strip())
    except ValueError:
        return None, {}
    rec = (await db.execute(select(BlockRecord).where(BlockRecord.id == rid))).scalar_one_or_none()
    if not rec:
        return None, {}
    items = (await db.execute(
        select(BlockRecordItem)
        .where(BlockRecordItem.block_record_id == rid, BlockRecordItem.status == BlockItemStatus.blocked)
        .order_by(BlockRecordItem.barcode)
    )).scalars().all()
    return rec, {it.device_id: it for it in items}


def _parse_price_groups(raw: str):
    """Validate the New Sale form's `price_groups` JSON.

    Returns {barcode: per-unit Decimal price} in tag order, or an error string.
    A tag may appear in only one group (it would otherwise be sold twice at two
    prices), every group needs at least one tag and a valid non-negative price."""
    import json
    try:
        groups = json.loads(raw)
    except ValueError:
        return "Invalid price groups — please reload the page and try again"
    if not isinstance(groups, list) or not groups:
        return "Add at least one tag group with a price"
    out: dict = {}
    for n, g in enumerate(groups, 1):
        if not isinstance(g, dict):
            return f"Group {n} is invalid"
        tags = [t.strip() for t in str(g.get("barcodes", "")).split(",") if t.strip()]
        if not tags:
            return f"Group {n} has no tag numbers"
        try:
            price = Decimal(str(g.get("price", "")).strip())
        except Exception:
            return f"Group {n} has an invalid price"
        if price < 0:
            return f"Group {n} has a negative price"
        for t in tags:
            if t in out:
                return f"Tag {t} appears in more than one group"
            out[t] = price
    return out


async def _sale_new_response(request: Request, barcode: str, barcodes: str, qty: int,
                             embed: int, db: AsyncSession, current_user: User,
                             template: str = "sales/new.html", block: dict | None = None,
                             error: str | None = None):
    """Shared by GET /sales/new (a single Sell link, or a small ?barcodes=
    list — still fine in a query string) and POST /sales/new (Multi-Sell
    with a large selection, which a GET would otherwise cram into the URL
    and risk a 414 Request-URI Too Large from the web server/proxy)."""
    device = None; lot = None; stage_error = None; approved_qty = None
    stock_price = None; prefill_barcode = None; prefill_qty = None; multi_count = 0

    # ── Multi-sell prefill: comma-separated tag list (barcodes=A,B&qty=N) ──
    if barcodes:
        codes, seen = [], set()
        for c in barcodes.split(","):
            c = c.strip()
            if c and c not in seen:
                seen.add(c); codes.append(c)
        if codes:
            prefill_barcode = ",".join(codes)
            prefill_qty = qty or len(codes)
            multi_count = len(codes)
            rows = (await db.execute(
                select(Device, Lot).outerjoin(Lot, Device.lot_id == Lot.id)
                .where(Device.barcode.in_(codes))
            )).all()
            stock_price = sum(_unit_stock_price(d, l) for d, l in rows)
            if rows:
                device, lot = rows[0]  # representative device for the info alert
    elif barcode:
        result = await db.execute(select(Device).where(Device.barcode == barcode))
        device = result.scalar_one_or_none()
        if device:
            lot_result = await db.execute(select(Lot).where(Lot.id == device.lot_id))
            lot = lot_result.scalar_one_or_none()
            # Show warning if not ready
            if device.current_stage != DeviceStage.ready_to_sale:
                stage_val = device.current_stage.value if device.current_stage else "unknown"
                stage_error = (f"Device is in stage '{stage_val}' — "
                               f"it must be in 'ready_to_sale' to sell.")
            # Prefill qty from the latest approved telecaller dispatch request
            appr = (await db.execute(
                select(TelecallerDispatchRequest)
                .where(TelecallerDispatchRequest.device_id == device.id,
                       TelecallerDispatchRequest.status == "approved")
                .order_by(TelecallerDispatchRequest.approved_at.desc())
            )).scalars().first()
            if appr:
                approved_qty = appr.qty_requested
            stock_price = _unit_stock_price(device, lot)
    next_num = await _next_sale_number(db)
    from utils.sales_person import sales_person_options
    return templates.TemplateResponse(template, {
        "request": request, "device": device, "lot": lot,
        "sales_person_options": await sales_person_options(db),
        "next_sale_number": next_num, "current_user": current_user,
        "error": stage_error, "approved_qty": approved_qty,
        "stock_price": stock_price, "prefill_barcode": prefill_barcode,
        "prefill_qty": prefill_qty, "multi_count": multi_count,
        "embed": bool(embed), "today": app_today().isoformat(),
        "block": block,
    } | ({"error": error} if error else {}))


@router.get("/sales/new", response_class=HTMLResponse)
async def sale_new_form(request: Request, barcode: str = None,
                        barcodes: str = None, qty: int = None,
                        embed: int = 0,
                        db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed)):
    return await _sale_new_response(request, barcode, barcodes, qty, embed, db, current_user)


MAX_TAG_INFO = 10000


@router.post("/sales/new-tag-sale/tag-info")
async def sale_new_tag_info(barcodes: str = Form(""),
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(allowed)):
    """Model / category / stage for the tags typed or scanned on New Tag Sale, so its
    per-tag price table can label each row and apply a price to a category.
    Category is the Ready to Sale page's definition (device type, else sub-
    category). Tags the DB does not know come back with found=false; the sale
    itself is still validated server-side on submit."""
    codes, seen = [], set()
    for c in barcodes.split(","):
        c = c.strip()
        if c and c not in seen:
            seen.add(c); codes.append(c)
    codes = codes[:MAX_TAG_INFO]
    rows = {}
    if codes:
        for d in (await db.execute(select(Device).where(Device.barcode.in_(codes)))).scalars().all():
            rows[d.barcode] = d
    out = []
    for c in codes:
        d = rows.get(c)
        if not d:
            out.append({"barcode": c, "found": False, "category": "—", "model": "", "stage": ""})
            continue
        out.append({
            "barcode": c, "found": True,
            "category": d.device_type or d.sub_category or "—",
            "model": " ".join(x for x in (d.brand, d.model) if x),
            "stage": getattr(d.current_stage, "value", d.current_stage) or "",
        })
    return JSONResponse({"tags": out})


# ── New Tag Sale — the per-tag-price sale page, a separate page so the original
# New Sale above stays exactly as it was. Same prefill + same POST /sales/new
# create logic (the form posts sale_page=tag so a failure re-renders THIS page).
@router.get("/sales/new-tag-sale", response_class=HTMLResponse)
async def sale_new_tag_form(request: Request, barcode: str = None,
                            barcodes: str = None, qty: int = None,
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(allowed)):
    return await _sale_new_response(request, barcode, barcodes, qty, 0, db, current_user,
                                    template="sales/new_tag_sale.html")


@router.post("/sales/new-tag-sale/prefill", response_class=HTMLResponse)
async def sale_new_tag_form_prefill(request: Request, barcode: str = Form(default=None),
                                    barcodes: str = Form(default=None), qty: int = Form(default=None),
                                    db: AsyncSession = Depends(get_db),
                                    current_user: User = Depends(allowed)):
    """POST twin of GET /sales/new-tag-sale for a large tag selection (a bulk sale
    can be thousands of tags; a query string could not carry them)."""
    return await _sale_new_response(request, barcode, barcodes, qty, 0, db, current_user,
                                    template="sales/new_tag_sale.html")


@router.post("/sales/new/prefill", response_class=HTMLResponse)
async def sale_new_form_prefill(request: Request, barcode: str = Form(default=None),
                                barcodes: str = Form(default=None), qty: int = Form(default=None),
                                embed: int = Form(default=0),
                                db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(allowed)):
    """POST twin of GET /sales/new, for Multi-Sell's (potentially large)
    selected-tag list — a GET with hundreds of barcodes crammed into the
    query string can trip a web server/proxy's max URI length (414 Request-
    URI Too Large); a POST body has no such limit. Renders the exact same
    prefilled New Sale page."""
    return await _sale_new_response(request, barcode, barcodes, qty, embed, db, current_user)


@router.post("/sales/new/block", response_class=HTMLResponse)
async def sale_new_from_block(request: Request, block_record_id: str = Form(""),
                              db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed),
                              _perm: User = Depends(require_module_perm("sales", "add"))):
    """Block Tags "Sell": the New Sale page prefilled with every still-blocked
    tag of one block — no cap — same page and same submit as As-Is Lot "Sell"
    on Ready for Sale. The block id travels with the form, so create_sale lets
    these tags through the Block Tags hold and closes it on each one sold."""
    rec, items = await _block_sale_context(db, block_record_id)
    if not rec:
        return await _sale_new_response(request, None, None, None, 0, db, current_user,
                                        error="Block not found — reload Block Tags and try again.")
    codes = [it.barcode for it in sorted(items.values(), key=lambda i: i.barcode)]
    block = {"id": str(rec.id), "block_id": rec.block_id, "count": len(codes)}
    if not codes:
        return await _sale_new_response(request, None, None, None, 0, db, current_user, block=block,
                                        error=f"Block {rec.block_id} has no blocked tags left to sell.")
    return await _sale_new_response(request, None, ",".join(codes), len(codes), 0, db,
                                    current_user, block=block)


@router.post("/sales/new")
async def create_sale(
    request: Request,
    background_tasks: BackgroundTasks,
    barcode: str = Form(""),
    sale_price: str = Form(""),
    price_groups: str = Form(""),
    sale_page: str = Form(""),
    customer_name: str = Form(""),
    sales_person: str = Form(""),
    customer_phone: str = Form(""),
    customer_state: str = Form(default=None),
    customer_address: str = Form(""),
    invoice_no: str = Form(""),
    payment_mode: str = Form("cash"),
    notes: str = Form(""),
    qty: int = Form(1),
    invoice_file_path: str = Form(""),
    warranty_type: str = Form("none"),
    sale_date: str = Form(""),
    block_record_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("sales", "add")),
):
    # The Ready to Sale page posts this form over AJAX so a bulk sale never leaves
    # the page. Those callers get JSON — including the real failure text, which an
    # embedded page could only render into a frame the user then had to read.
    # Ordinary form posts are unaffected and still redirect/render as before.
    xhr = request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"

    # Sold from Block Tags "Sell": tags still blocked under THIS block may be
    # sold despite the hold (selling them is the point of the block); every
    # other blocked tag is still refused as before.
    block_rec, block_items = await _block_sale_context(db, block_record_id)
    block_ctx = ({"id": str(block_rec.id), "block_id": block_rec.block_id, "count": len(block_items)}
                 if block_rec else None)

    async def _fail(message: str, device=None, lot=None, status: int = 400):
        if xhr:
            return JSONResponse({"ok": False, "error": message}, status_code=status)
        if block_ctx:
            # Keep the block's tags on the form after an error.
            return await _sale_new_response(request, None, barcode, None, 0, db, current_user,
                                            block=block_ctx, error=message)
        return templates.TemplateResponse(
            "sales/new_tag_sale.html" if sale_page == "tag" else "sales/new.html", {
            "request": request, "device": device, "lot": lot,
            "next_sale_number": await _next_sale_number(db),
            "current_user": current_user, "error": message,
            "today": app_today().isoformat(),
            # New Tag Sale keeps the tags on the form after an error: re-picking
            # thousands of tags would be a punishing way to report "no price".
            "prefill_barcode": (barcode or None) if sale_page == "tag" else None,
        })

    # ── Multi-sale support: barcode field may be a comma-separated tag list ──
    # One invoice can carry several price groups (different categories, or the
    # same category at different prices): `price_groups` is a JSON list of
    # {"barcodes": "A,B", "price": "<per-unit>"} and gives each tag its own
    # price. Every other field (invoice, customer, state, ...) is shared. Without
    # it the form behaves exactly as before: one price for every tag.
    price_by_code = {}
    if price_groups.strip():
        parsed = _parse_price_groups(price_groups)
        if isinstance(parsed, str):
            return await _fail(parsed)
        price_by_code = parsed
        codes = list(price_by_code)
    else:
        codes, _seen = [], set()
        for c in barcode.split(","):
            c = c.strip()
            if c and c not in _seen:
                _seen.add(c); codes.append(c)
    is_multi = len(codes) > 1

    if qty > 1 and not is_multi and not price_by_code:
        notes = f"[Qty:{qty}] {notes}".strip()

    if price_by_code:
        price = None      # per-tag from price_by_code
    else:
        try:
            price = Decimal(sale_price)  # per-unit price (form divides Total ÷ Qty)
        except Exception:
            return await _fail("Invalid sale price — please enter a valid number")

    # warranty_type is submitted as whatever raw label the admin has
    # configured in Master Data (sale_warranty_type) — "30 Days", "60 Days",
    # "6 Months", ... — not a fixed slug, so it's parsed generically rather
    # than checked against a hardcoded list (which silently defaulted every
    # sale, single or bulk, to No Warranty regardless of what was picked,
    # since e.g. "30 Days" never equals "30_days").
    wtype, warranty_days_selected = parse_warranty_duration(warranty_type)
    wtype = wtype or "none"

    # Sale Date defaults to now; a selected date keeps the current time-of-day
    # so ordering within a day and warranty-expiry math both still make sense
    # for a backdated entry.
    now_dt = app_now()
    resolved_sold_at = now_dt
    if sale_date:
        try:
            resolved_sold_at = datetime.combine(
                datetime.strptime(sale_date, "%Y-%m-%d").date(), now_dt.time())
        except ValueError:
            pass

    # ── Resolve the whole batch up front (set-based, not per device) ──────────
    # This loop used to issue ~10 sequential round trips PER DEVICE. Against a
    # database in another region that made a 600-tag bulk sale ~6,000 serial
    # round trips and it never finished inside a request timeout. Everything the
    # loop needs is now fetched in a fixed handful of queries regardless of size.
    devices_by_code = {
        d.barcode: d for d in (await db.execute(
            select(Device).where(Device.barcode.in_(codes))
        )).scalars().all()
    }
    found = [devices_by_code[c] for c in codes if c in devices_by_code]

    # Costing rows for below-cost warnings (2 queries for the whole batch).
    costings = await get_or_create_costings(found, db)

    # ── Selling company per device, resolved once for the whole batch ────────
    # Matches each device's entity (Deshwal / OxyPC Computers / Renew
    # Circuits / ...) to the Company Setting row tagged with that same
    # entity. Falls back to the oldest active company (same rule
    # get_company_settings already used everywhere) when a device's entity
    # has no matching company row, so a sale is never blocked for missing
    # company setup — it just prints with the same default as before this
    # change until an admin adds that entity's company.
    active_companies = (await db.execute(
        select(Company).where(Company.is_active == True).order_by(Company.created_at)
    )).scalars().all()
    company_by_entity = {}
    for c in active_companies:
        company_by_entity.setdefault(c.company_entity, c)
    fallback_company = active_companies[0] if active_companies else None

    def _company_snapshot_fields(company):
        if not company:
            return {"company_id": None, "company_name": None, "company_address": None,
                    "company_gstin": None, "company_state": None, "company_state_code": None,
                    "company_phone": None, "company_email": None}
        return {
            "company_id": company.id, "company_name": company.company_name,
            "company_address": company.company_address, "company_gstin": company.company_gstin,
            "company_state": company.company_state, "company_state_code": company.company_state_code,
            "company_phone": company.company_phone, "company_email": company.company_email,
        }

    # Open stage movements to close, keyed by device — one query, not one each.
    open_moves = {}
    if found:
        for mv in (await db.execute(
            select(StageMovement)
            .where(StageMovement.device_id.in_([d.id for d in found]),
                   StageMovement.exited_at == None)
            .order_by(StageMovement.moved_at.desc())
        )).scalars().all():
            # Keep the newest per (device, to_stage); ordering above is desc.
            open_moves.setdefault((mv.device_id, mv.to_stage), mv)

    # Sale numbers: drawn in one call instead of one nextval per device. Count is
    # the devices that will ACTUALLY sell, not the tags submitted — drawing per
    # submitted tag would burn a number for every skipped tag and leave holes in
    # the SALE- sequence. validate_sale_allowed is pure in-memory, so this
    # pre-pass costs no queries and keeps eligibility defined in one place.
    async def _check_sellable(d):
        if d.id in block_items:
            # Block hold waived for this block's own tags; stage still enforced.
            stage = getattr(d.current_stage, "value", d.current_stage)
            if stage != DeviceStage.ready_to_sale.value:
                raise HTTPException(status_code=403, detail=(
                    f"CONTROL ENGINE: Sale blocked — device is in stage '{stage}'. "
                    f"Device must be in 'ready_to_sale' before a sale can be created."))
            return
        await validate_sale_allowed(d)

    eligible = 0
    for d in found:
        try:
            await _check_sellable(d)
            eligible += 1
        except HTTPException:
            pass
    if eligible:
        await ensure_sale_number_seq(db)
    sale_numbers = [
        f"SALE-{n:04d}" for n in (await db.execute(
            text("SELECT nextval('sale_number_seq') FROM generate_series(1, :n)"),
            {"n": eligible},
        )).scalars().all()
    ] if eligible else []

    sold, skipped, warn, events = [], [], None, []
    _num_idx = 0
    for code in codes:
        device = devices_by_code.get(code)
        if not device:
            if not is_multi:
                return await _fail(f"Device {code} not found")
            skipped.append(f"{code} (not found)")
            continue

        # ── Control Engine: sale block ────────────────────────────────────
        try:
            await _check_sellable(device)
        except HTTPException as e:
            if not is_multi:
                return await _fail(e.detail, device=device)
            skipped.append(f"{code} (blocked)")
            continue

        # ── Cost Engine: below-cost warning (first one reported) ─────────
        price = price_by_code.get(code, price)
        w = below_cost_warning_for(costings.get(device.id), price)
        if w and not warn:
            warn = w

        sale_num = sale_numbers[_num_idx]
        _num_idx += 1
        sold_at = resolved_sold_at
        # Same shared parse as above — warranty_days_selected is the actual
        # day count for whatever Master Data label was picked (not limited to
        # the 3 legacy slugs compute_warranty_expiry's fixed map knows about).
        warranty_expires_at = (sold_at + timedelta(days=warranty_days_selected)
                               if warranty_days_selected else None)
        sale = Sale(
            sale_number=sale_num, device_id=device.id,
            sale_price=price,
            customer_name=customer_name or None, customer_phone=customer_phone or None,
            customer_state=customer_state or None, customer_address=customer_address or None,
            invoice_no=invoice_no or None, payment_mode=payment_mode,
            sold_by=current_user.username, sales_person=sales_person.strip() or None,
            notes=notes or None,
            invoice_file_path=invoice_file_path or None,
            sold_at=sold_at,
            warranty_type=wtype, warranty_days=warranty_days_selected,
            warranty_expires_at=warranty_expires_at,
            **_company_snapshot_fields(company_by_entity.get(device.entity, fallback_company)),
        )
        db.add(sale)

        prev = device.current_stage
        prev_mv = open_moves.get((device.id, prev))
        if prev_mv:
            prev_mv.exited_at = app_now()

        device.current_stage = DeviceStage.sold
        device.updated_at    = app_now()
        block_item = block_items.get(device.id)
        if block_item is not None:
            # Close the hold on this tag; the note links the sale to its block.
            device.is_blocked = False
            block_item.status = BlockItemStatus.released
            block_item.status_changed_by_user_id = current_user.id
            block_item.status_changed_at = datetime.utcnow()
            block_item.notes = SOLD_NOTE_PREFIX + sale_num
        db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=DeviceStage.sold,
                             moved_by=current_user.username, notes=f"Sold — {sale_num}"))

        await audit(db, user=current_user, action="SALE_CREATED",
                    table_name="sales", record_id=str(device.id),
                    new_value={"sale_number": sale_num, "price": str(price),
                               "below_cost": bool(w),
                               **({"block_id": block_rec.block_id} if block_item is not None else {})},
                    notes=w, request=request)

        sold.append(sale_num)
        events.append({
            "sale_number": sale_num,
            "barcode": code,
            "price": str(price),
            "customer_name": customer_name or None,
            "sold_by": current_user.username,
            "_source": "sales_html",
        })

    if not sold:
        return await _fail(
            "No sales recorded — " + ("; ".join(skipped) or "no valid tag numbers"))

    await db.commit()
    for payload in events:
        publish(EventType.SALE_COMPLETED, payload, background_tasks)

    if is_multi:
        redirect = f"/sales?success={len(sold)}+devices+sold"
    else:
        redirect = f"/sales?success=Sale+{sold[0]}+recorded"
    warn_parts = []
    if warn:
        warn_parts.append(warn)
    if skipped:
        warn_parts.append(f"{len(skipped)} skipped: " + ", ".join(skipped))
    if warn_parts:
        import urllib.parse
        redirect += f"&warning={urllib.parse.quote(' | '.join(warn_parts))}"
    if xhr:
        return JSONResponse({
            "ok": True, "sold": len(sold), "skipped": skipped,
            "warning": " | ".join(warn_parts) or None, "redirect": redirect,
        })
    return RedirectResponse(url=redirect, status_code=302)


MAX_SALES_DELETE = 2000


def _can_delete_sales(user: User) -> bool:
    # Admin-only on purpose: this permanently removes financial records, and the
    # Module Permission matrix defaults to "allow" for any role it has no row
    # for, so a matrix gate here would open it to far more roles than intended.
    return getattr(user.role, "value", user.role) == "admin"


@router.post("/sales/bulk-delete")
async def bulk_delete_sales(
    request: Request,
    sale_ids: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sales List's Delete Selected — PERMANENTLY removes the chosen sale records.

    Guard rails, because this is a hard delete of financial data:
      * admin only;
      * a sale that anything else references (credit-note Returns, customer
        receipts, care warranties/tickets/pairings — read live from the FK
        catalog) is SKIPPED and reported, never cascaded: deleting a sale must
        not silently take its credit notes or payment receipts with it;
      * each sale is audited (SALE_DELETED, with its old values) before removal;
      * a device left in "sold" with no sale behind it is moved back to the
        stage it was sold from, with a StageMovement, so inventory stays true.
    The uploaded invoice PDF, if any, is left on disk.
    """
    from sqlalchemy.exc import IntegrityError
    from utils.fk_purge import referenced_parent_ids

    if not _can_delete_sales(current_user):
        raise HTTPException(status_code=403, detail="Only an admin can permanently delete sales.")

    ids: list = []
    for raw in sale_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(_uuid.UUID(raw))
        except ValueError:
            continue
    ids = list(dict.fromkeys(ids))
    if not ids:
        return RedirectResponse(url="/sales?error=Select+at+least+one+sale", status_code=302)
    if len(ids) > MAX_SALES_DELETE:
        return RedirectResponse(
            url=f"/sales?error=Select+at+most+{MAX_SALES_DELETE}+sales+per+delete", status_code=302)

    sales = (await db.execute(select(Sale).where(Sale.id.in_(ids)))).scalars().all()
    blocked_ids = await referenced_parent_ids(db, "sales", [s.id for s in sales])
    deletable = [s for s in sales if s.id not in blocked_ids]
    blocked_n = len(sales) - len(deletable)

    deleted_n = reverted_n = 0
    if deletable:
        device_ids = {s.device_id for s in deletable}
        devices = {d.id: d for d in (await db.execute(
            select(Device).where(Device.id.in_(device_ids)))).scalars().all()}

        sale_nos_by_device: dict = {}
        for s in deletable:
            dev = devices.get(s.device_id)
            sale_nos_by_device.setdefault(s.device_id, []).append(s.sale_number)
            await audit(db, user=current_user, action="SALE_DELETED",
                        table_name="sales", record_id=str(s.id),
                        old_value={
                            "sale_number": s.sale_number,
                            "barcode": dev.barcode if dev else None,
                            "price": str(s.sale_price),
                            "customer": s.customer_name,
                            "invoice_no": s.invoice_no,
                            "sold_by": s.sold_by,
                            "sold_at": s.sold_at.isoformat() if s.sold_at else None,
                        },
                        request=request)
            await db.delete(s)
        await db.flush()
        deleted_n = len(deletable)

        # Devices that no longer have ANY sale and are still sitting in "sold".
        still_sold_ids = set((await db.execute(
            select(Sale.device_id).where(Sale.device_id.in_(device_ids)).distinct()
        )).scalars().all())
        to_revert = [d for did, d in devices.items()
                     if did not in still_sold_ids and d.current_stage == DeviceStage.sold]
        if to_revert:
            latest_into_sold = {
                mv.device_id: mv for mv in (await db.execute(
                    select(StageMovement)
                    .where(StageMovement.device_id.in_([d.id for d in to_revert]),
                           StageMovement.to_stage == DeviceStage.sold)
                    .order_by(StageMovement.device_id, StageMovement.moved_at.desc())
                    .distinct(StageMovement.device_id)
                )).scalars().all()
            }
            for dev in to_revert:
                mv = latest_into_sold.get(dev.id)
                back_to = (mv.from_stage if mv and mv.from_stage and mv.from_stage != DeviceStage.sold
                           else DeviceStage.ready_to_sale)
                if mv and mv.exited_at is None:
                    mv.exited_at = app_now()
                dev.current_stage = back_to
                dev.updated_at = app_now()
                db.add(StageMovement(
                    device_id=dev.id, from_stage=DeviceStage.sold, to_stage=back_to,
                    moved_by=current_user.username,
                    notes=f"Sale deleted — {', '.join(sale_nos_by_device.get(dev.id, []))}"))
                reverted_n += 1

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return RedirectResponse(
                url="/sales?error=Could+not+delete+—+a+selected+sale+is+still+referenced+elsewhere",
                status_code=302)

    qs = []
    if deleted_n:
        msg = f"{deleted_n} sale(s) permanently deleted"
        if reverted_n:
            msg += f"; {reverted_n} tag(s) moved back out of Sold"
        qs.append("success=" + quote_plus(msg))
    skipped_msgs = []
    if blocked_n:
        skipped_msgs.append(f"{blocked_n} not deleted — linked to credit notes, receipts or warranty records")
    missing_n = len(ids) - len(sales)
    if missing_n:
        skipped_msgs.append(f"{missing_n} not found")
    if skipped_msgs:
        qs.append("error=" + quote_plus("; ".join(skipped_msgs)))
    return RedirectResponse(url="/sales" + ("?" + "&".join(qs) if qs else ""), status_code=302)


@router.get("/sales/export-selected", response_class=HTMLResponse)
async def export_selected_get(request: Request, current_user: User = Depends(allowed)):
    """Redirect GET to sales list (form should POST)."""
    return RedirectResponse(url="/sales", status_code=302)


@router.post("/sales/export-selected")
async def export_selected_sales(
    sale_ids: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Export selected sales rows as CSV. Receives comma-separated Sale UUIDs."""
    from routers.reports import _latest_transfer_type_by_device

    ids = [sid.strip() for sid in sale_ids.split(",") if sid.strip()]
    if not ids:
        return RedirectResponse(url="/sales", status_code=302)

    result = await db.execute(
        select(Sale, Device.id.label("device_id"), Device.barcode, Device.brand, Device.model,
               Device.grade, Device.sub_category, Lot.lot_number)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Sale.id.in_(ids))
        .order_by(Sale.sold_at.desc())
    )
    rows = result.all()
    stage_by_device = await _latest_transfer_type_by_device(db, [row.device_id for row in rows])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Sale#", "Invoice Number", "Date", "Barcode", "Brand", "Model", "Lot", "Grade",
                     "Price", "Customer", "Phone", "Payment", "Sold By", "Sales Person", "Category", "Stage", "Remarks"])
    for row in rows:
        s = row.Sale
        transfer_type = stage_by_device.get(row.device_id)
        writer.writerow([
            s.sale_number,
            s.invoice_no or "",
            s.sold_at.strftime("%d-%m-%Y"),
            row.barcode, row.brand, row.model, row.lot_number,
            row.grade.value if row.grade else "",
            float(s.sale_price or 0),
            s.customer_name or "", s.customer_phone or "",
            s.payment_mode or "", s.sold_by or "", s.sales_person or "", row.sub_category or "",
            (transfer_type or "").replace("_", " ").title(), s.notes or "",
        ])
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sales_selected.csv"},
    )


@router.post("/sales/parse-invoice-pdf")
async def parse_invoice_pdf(
    pdf: UploadFile = File(...),
    current_user=Depends(allowed),
):
    """Accept a PDF upload, save it, and attempt best-effort field extraction."""
    upload_dir = Path("uploads/sale_invoices")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"inv_{int(time.time())}_{pdf.filename}"
    dest = upload_dir / safe_name
    with open(dest, "wb") as f:
        shutil.copyfileobj(pdf.file, f)

    result: dict = {"file_path": str(dest).replace("\\", "/")}

    # Best-effort text extraction — works if pdfplumber is installed
    try:
        import pdfplumber
        with pdfplumber.open(dest) as doc:
            text_content = " ".join(p.extract_text() or "" for p in doc.pages)

        # Total / sale price
        m = re.search(r"(?i)total\s*[:\s]+(?:INR|Rs\.?|₹)?\s*([\d,]+\.?\d*)", text_content)
        if m:
            result["sale_price"] = m.group(1).replace(",", "")

        # Customer / bill-to name
        m = re.search(r"(?i)(?:customer|bill\s*to|sold\s*to)\s*[:\s]+([A-Z][^\n]{2,50})", text_content)
        if m:
            result["customer_name"] = m.group(1).strip()

        # Invoice / PO number
        m = re.search(r"(?i)(?:invoice\s*no|inv\.?\s*no|po\s*number)\s*[.:\s]+([A-Z0-9/_-]+)", text_content)
        if m:
            result["invoice_no"] = m.group(1).strip()
    except ImportError:
        pass  # pdfplumber not installed — file is saved, fields will be empty
    except Exception:
        pass  # extraction failed — still return file_path

    return JSONResponse(result)


# NOTE: /sales/data MUST stay above /sales/{sale_id}. FastAPI matches in
# registration order, so the parameterised route would otherwise capture
# "data" as a sale id and 404.
def _parse_simple_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _sales_filters(q, sale_no, sold_by_filter, customer, grade, lot_id, date_from="", date_to=""):
    """Filter clauses shared by the Sales list page, its data endpoint, and
    its CSV export, so none of the three can ever disagree about what the
    filters mean."""
    from sqlalchemy import or_ as _or
    w = []
    if q:
        like = f"%{q}%"
        w.append(_or(Device.barcode.ilike(like), Device.brand.ilike(like),
                     Device.model.ilike(like)))
    if sale_no:
        w.append(Sale.sale_number.ilike(f"%{sale_no}%"))
    if sold_by_filter:
        w.append(Sale.sold_by == sold_by_filter)
    if customer:
        w.append(Sale.customer_name.ilike(f"%{customer}%"))
    if grade:
        w.append(Device.grade == grade)
    if lot_id:
        w.append(Device.lot_id == lot_id)
    d_from = _parse_simple_date(date_from)
    if d_from:
        w.append(Sale.sold_at >= d_from)
    d_to = _parse_simple_date(date_to)
    if d_to:
        w.append(Sale.sold_at <= d_to.replace(hour=23, minute=59, second=59))
    return w


def _sales_search_filter(search: str):
    """DataTables' own search-box clause, factored out of /sales/data so
    /sales/ids (Select All) can never disagree with it about what the live
    search box matches."""
    if not search:
        return []
    like = f"%{search}%"
    return [or_(
        Device.barcode.ilike(like), Device.brand.ilike(like),
        Device.model.ilike(like), Sale.sale_number.ilike(like),
        Sale.customer_name.ilike(like), Sale.sales_person.ilike(like),
        Sale.sold_by.ilike(like), Lot.lot_number.ilike(like),
        Sale.invoice_no.ilike(like),
        # Block ID of a sale made from Block Tags "Sell".
        Sale.sale_number.in_(
            select(func.substr(BlockRecordItem.notes, len(SOLD_NOTE_PREFIX) + 1))
            .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
            .where(BlockRecord.block_id.ilike(like),
                   BlockRecordItem.notes.like(SOLD_NOTE_PREFIX + "%"))),
    )]


@router.get("/sales/data")
async def sales_list_data(
    request: Request,
    draw: int = Query(default=1),
    start: int = Query(default=0),
    length: int = Query(default=25),
    q: str = Query(default=""),
    sale_no: str = Query(default=""),
    sold_by_filter: str = Query(default=""),
    customer: str = Query(default=""),
    grade: str = Query(default=""),
    lot_id: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """DataTables server-side feed for the Sales list.

    The page used to render every sale into the HTML. At ~1,000 sales that was
    fine; after the July backfill took it to ~9,900 the response reached 17 MB
    and would hang a browser tab. Paging over the wire instead keeps the
    response a few tens of KB.

    This deliberately does NOT reintroduce the problem fixed on 2026-07-07,
    where server-side paging hid records behind a page cap: DataTables' own
    search and paging drive this endpoint, so every record is still reachable
    from the table — the difference is only how much travels per request.
    """
    from sqlalchemy import desc as _desc, asc as _asc
    from models.role_permissions import can_view_pricing as _cvp

    role = getattr(current_user.role, "value", current_user.role)
    show_pricing = _cvp(role)

    base_join = (
        select(Sale, Device.barcode, Device.brand, Device.model, Device.grade,
               Lot.lot_number, Lot.buying_price, Lot.qty, Device.sub_lot_number,
               Device.device_price, Device.id.label("device_id"), Device.replaced,
               Device.entity)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
    )
    count_join = (
        select(func.count())
        .select_from(Sale)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
    )
    revenue_join = (
        select(func.coalesce(func.sum(Sale.sale_price), 0))
        .select_from(Sale)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
    )

    page_filters = _sales_filters(q, sale_no, sold_by_filter, customer, grade, lot_id, date_from, date_to)

    # DataTables' own search box, on top of the page's filter bar.
    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = _sales_search_filter(search)

    total = (await db.execute(count_join.where(*page_filters))).scalar() or 0
    filtered = (await db.execute(
        count_join.where(*page_filters, *search_filters))).scalar() or 0
    revenue = float((await db.execute(
        revenue_join.where(*page_filters, *search_filters))).scalar() or 0)

    # Sorting. Only columns with a real SQL expression are sortable; anything
    # else falls back to sale date so an unmapped index cannot 500 the table.
    # Column 7 is Stock Type (after Grade), so every column after it sits one
    # place later than before: with pricing (Cost/Unit, Price, Margin at 8-10)
    # Customer=11, Payment=12, By=13, Sales Person=14; without pricing
    # Customer=8, Payment=9, By=10, Sales Person=11.
    # Column 2 is Invoice (2026-10-10, after Sale#), so every index from Date
    # onward is one later again than the note above describes.
    stock_type_expr = case((func.coalesce(Device.sub_lot_number, "") != "", 1), else_=0)
    col_map = {1: Sale.sale_number, 2: Sale.invoice_no, 3: Sale.sold_at, 4: Device.barcode,
               7: Device.grade, 8: stock_type_expr}
    if show_pricing:
        col_map.update({10: Sale.sale_price, 13: Sale.payment_mode, 14: Sale.sold_by, 15: Sale.sales_person})
    else:
        col_map.update({10: Sale.payment_mode, 11: Sale.sold_by, 12: Sale.sales_person})
    try:
        order_col = int(request.query_params.get("order[0][column]", 3))
    except ValueError:
        order_col = 3
    order_dir = request.query_params.get("order[0][dir]", "desc")
    sort_expr = col_map.get(order_col, Sale.sold_at)
    order_by = _asc(sort_expr) if order_dir == "asc" else _desc(sort_expr)

    rows = (await db.execute(
        base_join.where(*page_filters, *search_filters)
        .order_by(order_by, Sale.sale_number)   # tie-break, so paging is stable
        .offset(max(0, start)).limit(min(max(1, length), 500))
    )).all()

    parts_badges = await parts_badge_map(db, [r.device_id for r in rows])
    block_by_sale = await block_id_by_sale_number(db, [r.Sale.sale_number for r in rows])

    def esc(v):
        from html import escape
        return escape(str(v)) if v is not None else ""

    data = []
    for r in rows:
        s = r.Sale
        gv = getattr(r.grade, "value", r.grade) or "—"
        # Replace Now (Product Return) replacement sale: same test the Returns
        # page uses — Device.replaced "Replaced with <tag>" and a ₹0 sale.
        repl_text = (r.replaced or "").strip()
        is_replacement = repl_text.startswith("Replaced with ") and not float(s.sale_price or 0)
        invoice_cell = (f'<span class="font-monospace">{esc(s.invoice_no)}</span>' if s.invoice_no
                        else '<span class="text-muted">—</span>')
        if is_replacement:
            repl_badge = f'<span class="badge bg-info text-dark">{esc(repl_text)}</span>'
            # No "—" above the badge when there is no invoice number.
            invoice_cell = (f'<span class="font-monospace">{esc(s.invoice_no)}</span><br>{repl_badge}'
                            if s.invoice_no else repl_badge)
        cells = [
            f'<input type="checkbox" class="row-check" value="{s.id}">',
            (f'<a href="/sales/{s.id}" class="text-decoration-none fw-semibold text-nowrap">{esc(s.sale_number)}</a>'
             + (f'<div class="small text-muted text-nowrap" title="Sold from Block Tags">'
                f'(Box - {esc(block_by_sale[s.sale_number])})</div>'
                if s.sale_number in block_by_sale else '')),
            invoice_cell,
            s.sold_at.strftime("%d-%m-%Y") if s.sold_at else "—",
            (f'<span class="text-nowrap"><a href="/devices/{esc(r.barcode)}" class="text-decoration-none"><code>{esc(r.barcode)}</code></a>'
             + parts_badges.get(str(r.device_id), '') + '</span>'
             + (f'<div class="small text-muted">{esc(r.entity)}</div>' if r.entity else '')),
            esc(f"{r.brand or ''} {r.model or ''}".strip()),
            f'<a href="/devices?lot={esc(r.lot_number)}" class="text-decoration-none">'
            f'<span class="badge bg-info text-dark">{esc(r.lot_number)}</span></a>',
            esc(gv),
            # Stock Type: "As-Is Lot" when the tag has a sub-lot, else "Finish Good"
            ('<span class="badge bg-warning text-dark">As-Is Lot</span>' if (r.sub_lot_number or "").strip()
             else '<span class="badge bg-success">Finish Good</span>'),
        ]
        if show_pricing:
            # Same rule as Ready for Sale's Unit Cost (services/ready_pricing.
            # unit_price): the tag's own Device Price when set, otherwise the
            # lot average (buying price / qty), marked "avg".
            if r.device_price:
                cost_unit, cost_is_avg = float(r.device_price), False
            elif r.qty and r.qty > 0 and r.buying_price:
                cost_unit, cost_is_avg = float(r.buying_price) / r.qty, True
            else:
                cost_unit, cost_is_avg = 0.0, False
            price = float(s.sale_price or 0)
            margin = price - cost_unit
            mcls = "text-success" if margin > 0 else ("text-danger" if margin < 0 else "text-muted")
            avg_tag = (' <span class="text-muted ms-1" style="font-size:.7rem">avg</span>'
                       if cost_is_avg else '')
            cells += [
                (f'<span class="text-nowrap fw-semibold">₹{cost_unit:,.0f}{avg_tag}</span>'
                 if cost_unit else '<span class="text-muted">—</span>'),
                f'<span class="fw-semibold text-success">₹{price:,.0f}</span>',
                (f'<span class="fw-semibold {mcls}">{"+" if margin >= 0 else ""}₹{margin:,.0f}</span>'
                 if cost_unit else '<span class="text-muted">—</span>'),
            ]
        cells += [
            esc(s.customer_name or "—"),
            ('<span class="badge bg-info text-dark">Replaced</span>' if is_replacement
             else f'<span class="badge bg-secondary">{esc(s.payment_mode or "—")}</span>'),
            f'<span class="text-muted">{esc(s.sold_by or "—")}</span>',
            esc(s.sales_person or "—"),
            (f'<a href="/sales/{s.id}/download-invoice" class="btn btn-outline-secondary btn-sm py-0 px-1" '
             f'title="Download Invoice"><i class="bi bi-download"></i></a>'
             if s.invoice_file_path else '<span class="text-muted small">—</span>'),
            (f'<div class="d-flex gap-1">'
             f'<a href="/sales/{s.id}" class="btn btn-outline-info btn-sm py-0 px-1" title="View Detail"><i class="bi bi-eye"></i></a>'
             f'<a href="/invoices/print/{s.id}" target="_blank" class="btn btn-outline-secondary btn-sm py-0 px-1" title="Invoice"><i class="bi bi-receipt"></i></a>'
             f'<a href="/invoices/waybill/{s.id}" target="_blank" class="btn btn-outline-primary btn-sm py-0 px-1" title="Waybill"><i class="bi bi-truck"></i></a>'
             f'</div>'),
        ]
        data.append(cells)

    return JSONResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": filtered,
        "data": data,
        # Revenue for everything the filters match, not just this page — the
        # footer total would otherwise only add up the 25 rows on screen.
        "revenueFiltered": revenue,
        "showPricing": show_pricing,
    })


# NOTE: /sales/export MUST also stay above /sales/{sale_id} for the same
# reason /sales/data does — FastAPI would otherwise match "export" as a
# sale id and 404.
@router.get("/sales/export")
async def export_sales_filtered(
    q: str = Query(default=""),
    sale_no: str = Query(default=""),
    sold_by_filter: str = Query(default=""),
    customer: str = Query(default=""),
    grade: str = Query(default=""),
    lot_id: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """CSV export of exactly what the /sales table is currently showing —
    same _sales_filters() the page and /sales/data use, so this can never
    drift from the filtered view. Distinct from /reports/export/sales
    ("Export All CSV", always unfiltered) and POST /sales/export-selected
    (only the checked rows)."""
    from routers.reports import _latest_transfer_type_by_device

    MAX_EXPORT_ROWS = 5000
    rows = (await db.execute(
        select(Sale, Device.id.label("device_id"), Device.barcode, Device.brand, Device.model,
               Device.grade, Device.sub_category, Lot.lot_number)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .where(*_sales_filters(q, sale_no, sold_by_filter, customer, grade, lot_id, date_from, date_to))
        .order_by(Sale.sold_at.desc())
        .limit(MAX_EXPORT_ROWS)
    )).all()
    stage_by_device = await _latest_transfer_type_by_device(db, [row.device_id for row in rows])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Sale#", "Invoice Number", "Date", "Barcode", "Brand", "Model", "Lot", "Grade",
                     "Price", "Customer", "Phone", "Payment", "Sold By", "Sales Person", "Category", "Stage", "Remarks"])
    for row in rows:
        s = row.Sale
        transfer_type = stage_by_device.get(row.device_id)
        writer.writerow([
            s.sale_number, s.invoice_no or "", s.sold_at.strftime("%d-%m-%Y") if s.sold_at else "",
            row.barcode, row.brand, row.model, row.lot_number,
            row.grade.value if row.grade else "",
            float(s.sale_price or 0),
            s.customer_name or "", s.customer_phone or "",
            s.payment_mode or "", s.sold_by or "", s.sales_person or "", row.sub_category or "",
            (transfer_type or "").replace("_", " ").title(), s.notes or "",
        ])
    if len(rows) == MAX_EXPORT_ROWS:
        writer.writerow(["# TRUNCATED", f"Export capped at {MAX_EXPORT_ROWS} rows",
                         "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sales_filtered.csv"},
    )


# NOTE: /sales/ids MUST also stay above /sales/{sale_id}, same reason as
# /sales/data and /sales/export above.
@router.get("/sales/ids")
async def sales_list_ids(
    search: str = Query(default=""),
    q: str = Query(default=""),
    sale_no: str = Query(default=""),
    sold_by_filter: str = Query(default=""),
    customer: str = Query(default=""),
    grade: str = Query(default=""),
    lot_id: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Every Sale id matching the filter bar + DataTables search box, for
    "Select All" on the Sales list. Mirrors the fix already in place for the
    Ready to Sale table (/sales/ready/barcodes): with server-side paging only
    the current page's checkboxes exist in the DOM, so a plain
    querySelectorAll-based Select All only ever reached the page in view.
    Capped the same as the filtered CSV export so an unfiltered Select All on
    a huge table can't pull in an unbounded result set."""
    MAX_SELECT_ALL = 5000
    ids = (await db.execute(
        select(Sale.id)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .where(
            *_sales_filters(q, sale_no, sold_by_filter, customer, grade, lot_id, date_from, date_to),
            *_sales_search_filter(search.strip()),
        )
        .order_by(Sale.sold_at.desc())
        .limit(MAX_SELECT_ALL)
    )).scalars().all()
    return JSONResponse({"ids": [str(i) for i in ids], "truncated": len(ids) == MAX_SELECT_ALL})


@router.get("/sales/{sale_id}", response_class=HTMLResponse)
async def sale_detail(
    sale_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    # UUID columns require proper UUID type — bare string comparison fails with asyncpg
    try:
        sale_uuid = _uuid.UUID(sale_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Sale not found")

    result = await db.execute(
        select(Sale, Device, Lot)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Sale.id == sale_uuid)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Sale not found")
    sale, device, lot = row.Sale, row.Device, row.Lot
    block_id = (await block_id_by_sale_number(db, [sale.sale_number])).get(sale.sale_number)
    return templates.TemplateResponse("sales/detail.html", {
        "request": request,
        "current_user": current_user,
        "sale": sale,
        "device": device,
        "lot": lot,
        "block_id": block_id,
    })


DELIVERY_STATUSES = ("pending", "dispatched", "delivered")


@router.post("/sales/{sale_id}/transport")
async def sale_update_transport(
    sale_id: str,
    request: Request,
    transport_mode: str = Form(""),
    transport_via: str = Form(""),
    tracking_number: str = Form(""),
    dispatch_date: str = Form(""),
    delivery_status: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Sale Detail -> Update Transport. Transport Mode and Delivery Courier
    come from Master Data (transport_mode / delivery_courier); Tracking Number
    is free text. Blank fields clear the stored value."""
    try:
        sale_uuid = _uuid.UUID(sale_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Sale not found")
    sale = (await db.execute(select(Sale).where(Sale.id == sale_uuid))).scalar_one_or_none()
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    back = f"/sales/{sale_id}"
    status = (delivery_status or "").strip().lower()
    if status and status not in DELIVERY_STATUSES:
        return RedirectResponse(url=f"{back}?error=Invalid+delivery+status", status_code=302)
    when = None
    if dispatch_date.strip():
        try:
            when = datetime.strptime(dispatch_date.strip(), "%Y-%m-%d")
        except ValueError:
            return RedirectResponse(url=f"{back}?error=Invalid+dispatch+date", status_code=302)
    old = {"transport_mode": sale.transport_mode, "transport_via": sale.transport_via,
           "tracking_number": sale.tracking_number, "delivery_status": sale.delivery_status,
           "dispatch_date": sale.dispatch_date.isoformat() if sale.dispatch_date else None}
    sale.transport_mode = transport_mode.strip()[:30] or None
    sale.transport_via = transport_via.strip()[:100] or None
    sale.tracking_number = tracking_number.strip()[:100] or None
    sale.delivery_status = status or None
    sale.dispatch_date = when
    await audit(db, user=current_user, action="SALE_TRANSPORT_UPDATED", table_name="sales",
                record_id=str(sale.id), old_value=old,
                new_value={"transport_mode": sale.transport_mode, "transport_via": sale.transport_via,
                           "tracking_number": sale.tracking_number, "delivery_status": sale.delivery_status,
                           "dispatch_date": dispatch_date.strip() or None},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Transport+details+updated", status_code=302)


@router.get("/sales", response_class=HTMLResponse)
async def sales_list(
    request: Request,
    q: str = Query(default=""),
    sale_no: str = Query(default=""),
    sold_by_filter: str = Query(default=""),
    customer: str = Query(default=""),
    grade: str = Query(default=""),
    lot_id: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    from sqlalchemy import case as sa_case

    # Rows are fetched by /sales/data (DataTables server-side), not here. This
    # page previously rendered all ~9,900 sales inline, producing a 17 MB
    # response that hung the browser. Only the count is needed now, for the
    # header stat.
    total = (await db.execute(
        select(func.count())
        .select_from(Sale)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .where(*_sales_filters(q, sale_no, sold_by_filter, customer, grade, lot_id, date_from, date_to))
    )).scalar() or 0
    sales = []

    # ── Registered device stats (single query) ───────────────────────────────
    dev_stats = (await db.execute(
        select(
            func.count(Device.id).label("total"),
            func.count(sa_case((Device.current_stage == DeviceStage.sold, 1))).label("sold"),
        ).where(Device.is_active == True, Device.is_trashed == False)
    )).one()
    total_registered = dev_stats.total
    total_devices_sold = dev_stats.sold
    total_available = total_registered - total_devices_sold

    # ── Sales-user dropdown ──────────────────────────────────────────────────
    sellers_result = await db.execute(
        select(Sale.sold_by).distinct().where(Sale.sold_by.isnot(None)).order_by(Sale.sold_by)
    )
    sellers = [r.sold_by for r in sellers_result]

    # ── Lot dropdown ─────────────────────────────────────────────────────────
    lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
    lots = lots_result.scalars().all()

    return templates.TemplateResponse("sales/list.html", {
        "request": request,
        "sales": sales,
        "lots": lots,
        "sellers": sellers,
        "selected_lot": lot_id,
        "current_user": current_user,
        "total": total,
        "can_delete_sales": _can_delete_sales(current_user),
        # Filters
        "q": q,
        "sale_no": sale_no,
        "sold_by_filter": sold_by_filter,
        "customer": customer,
        "grade": grade,
        "date_from": date_from,
        "date_to": date_to,
        # Device stats
        "total_registered": total_registered,
        "total_devices_sold": total_devices_sold,
        "total_available": total_available,
    })


@router.get("/returns", response_class=HTMLResponse)
async def returns_list(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(allowed)):
    # Device.entity + the Sale customer fields are pulled in here (not just
    # barcode/brand/model/sale_price/sale_number as before) so the Receipt
    # modal can render entirely from this one row — no per-receipt query.
    result = await db.execute(
        select(Return, Device.barcode, Device.brand, Device.model, Device.entity, Device.serial_no,
               Sale.sale_price, Sale.sale_number, Sale.customer_name,
               Sale.customer_phone, Sale.customer_address)
        .join(Device, Return.device_id == Device.id)
        .join(Sale, Return.sale_id == Sale.id)
        .order_by(Return.return_date.desc())
    )
    returns = result.all()

    # Replace Now (Product Return) replacement rows — these tags were never
    # "returned" (no Return record exists for them), they were sold at ₹0 as
    # a replacement for a returned tag. Device.replaced, prefixed "Replaced
    # with " by process_return above, flags them; merged into the same
    # table — DataTables re-sorts both blocks together by Return Date on init.
    repl_result = await db.execute(
        select(Device.barcode, Device.brand, Device.model, Device.entity, Device.replaced, Device.serial_no,
               Sale.sale_price, Sale.sale_number, Sale.sold_at, Sale.sold_by,
               Sale.customer_name, Sale.customer_phone, Sale.customer_address)
        .join(Sale, Sale.device_id == Device.id)
        .where(Device.replaced.like('Replaced with %'), Sale.sale_price == 0)
        .order_by(Sale.sold_at.desc())
    )
    replacements = repl_result.all()

    return templates.TemplateResponse("sales/returns_list.html", {
        "request": request, "returns": returns, "replacements": replacements,
        "current_user": current_user,
    })


@router.get("/returns/{return_id}/receipt", response_class=HTMLResponse)
async def return_receipt(return_id: str, request: Request, db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(allowed)):
    """Standalone printable receipt — the Product Return list's Receipt
    button opens this in a new tab; a browser's own Print -> Save as PDF is
    the "downloadable copy". Quantity is always 1 (a Return is one device);
    Serial Number reuses Return.serial_captured (already existed, previously
    only ever populated with the barcode for Internal returns)."""
    try:
        rid = _uuid.UUID(return_id)
    except ValueError:
        raise HTTPException(404, "Return not found")
    row = (await db.execute(
        select(Return, Device.barcode, Device.brand, Device.model, Device.entity,
               Sale.customer_name, Sale.customer_phone, Sale.customer_address)
        .join(Device, Return.device_id == Device.id)
        .join(Sale, Return.sale_id == Sale.id)
        .where(Return.id == rid)
    )).first()
    if not row:
        raise HTTPException(404, "Return not found")
    ret, barcode, brand, model, entity, cust_name, cust_phone, cust_address = row
    return templates.TemplateResponse("sales/return_receipt.html", {
        "request": request, "current_user": current_user, "ret": ret,
        "barcode": barcode, "brand": brand, "model": model, "entity": entity,
        "customer_name": cust_name, "customer_phone": cust_phone, "customer_address": cust_address,
    })


async def _return_received_by_options(db: AsyncSession) -> list:
    """Active users' display names, for the Return New page's "Return
    Received By" dropdown (all three tabs)."""
    names = (await db.execute(
        select(User.full_name).where(User.status == True).order_by(User.full_name)
    )).scalars().all()
    return [n.strip() for n in names if n and n.strip()]


async def _return_form_response(db: AsyncSession, ctx: dict):
    """Render the Return New page, always with the Return Received By options,
    including when a submit is rejected and the form is shown again."""
    ctx.setdefault("return_received_by_options", await _return_received_by_options(db))
    return templates.TemplateResponse("sales/return_form.html", ctx)


async def _valid_received_by(db: AsyncSession, value: str) -> str:
    """The submitted Return Received By name if it is an active user's display
    name (matched case-insensitively, returned in its stored spelling), else ""."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    for n in await _return_received_by_options(db):
        if n.lower() == v:
            return n
    return ""


@router.get("/returns/new", response_class=HTMLResponse)
async def return_form(request: Request, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(allowed)):
    from models.cost_config import CostConfig
    row = (await db.execute(
        select(CostConfig).where(CostConfig.key == "default_paid_repair")
    )).scalar_one_or_none()
    default_paid_repair = float(row.value) if row else 1500.0
    return await _return_form_response(db, {
        "request": request, "current_user": current_user, "sale": None,
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
        "default_paid_repair": default_paid_repair,
        "active_tab": request.query_params.get("active_tab", "internal"),
    })


@router.post("/returns/new")
async def process_return(
    request: Request,
    barcode: str = Form(...),
    reason: str = Form(""),
    condition_on_return: str = Form(""),
    action_taken: str = Form("restock"),
    refund_amount: str = Form("0"),
    notes: str = Form(""),
    return_type: str = Form("customer"),
    replace_now: str = Form("no"),
    replace_tag: str = Form(""),
    complaint_text: str = Form(None),
    return_received_by: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("returns", "add")),
):
    # Matched case-insensitively — same fix as the CSV bulk tag upload and
    # Replace Now searchbox below: tag numbers get scanned/typed in whatever
    # case, while the stored barcode may differ, so an exact `==` here
    # reported "not found" for tags the searchbox (which uses .ilike()) had
    # just confirmed exist and are sitting in the right stage.
    dev_result = await db.execute(
        select(Device).where(func.upper(Device.barcode) == barcode.strip().upper())
    )
    device = dev_result.scalars().first()
    if not device:
        return await _return_form_response(db, {
            "request": request, "current_user": current_user,
            "error": f"Device {barcode} not found", "sale": None,
        })

    sale_result = await db.execute(
        select(Sale).where(Sale.device_id == device.id)
        .order_by(Sale.sold_at.desc()).limit(1)
    )
    sale = sale_result.scalars().first()
    placeholder_created = False
    if not sale and device.current_stage != DeviceStage.sold:
        # Device was never marked Sold at all — there's genuinely nothing to
        # return against, unlike the sold-with-no-sale-row case handled below.
        return await _return_form_response(db, {
            "request": request, "current_user": current_user,
            "error": "No sale found for this device", "sale": None,
        })
    if not sale:
        # Device shows current_stage == sold with zero Sale rows — it was
        # moved to Sold through a generic stage-mover instead of an actual
        # sale (DROPDOWN_STAGES used to allow this; fixed 2026-09-21, but
        # devices already stuck that way from before the fix still hit this).
        # Per explicit instruction: still process the return/replace rather
        # than block it — attach it to a placeholder Sale with blank customer
        # details (there is no real customer data to carry), so the return
        # (and, if Replace Now is used, the replacement) can still go through.
        sale_num = await _next_sale_number(db)
        last_sold_move = (await db.execute(
            select(StageMovement.moved_at)
            .where(StageMovement.device_id == device.id, StageMovement.to_stage == DeviceStage.sold)
            .order_by(StageMovement.moved_at.desc()).limit(1)
        )).scalar()
        sale = Sale(
            sale_number=sale_num, device_id=device.id, sale_price=Decimal("0"),
            sold_by=current_user.username, sold_at=last_sold_move or app_now(),
            warranty_type="none",
            notes=("Placeholder sale — device was marked Sold with no sale record on "
                   "file; auto-created so this return could be processed."),
        )
        db.add(sale)
        await db.flush()
        placeholder_created = True
        await audit(db, user=current_user, action="RETURN_PLACEHOLDER_SALE_CREATED",
                    table_name="sales", record_id=str(device.id),
                    new_value={"sale_number": sale_num, "reason": "sold stage with no sale record"},
                    request=request)

    # Guard: prevent duplicate return for the same sale
    existing_return = (await db.execute(
        select(Return).where(Return.sale_id == sale.id)
    )).scalars().first()
    if existing_return:
        return await _return_form_response(db, {
            "request": request, "current_user": current_user,
            "error": (f"A return for sale {sale.sale_number} already exists "
                      f"(processed on {existing_return.return_date.strftime('%d %b %Y')}). "
                      "Cannot create a duplicate return."),
            "sale": sale,
        })

    # Mandatory field validation
    if not reason or not reason.strip():
        return await _return_form_response(db, {
            "request": request, "current_user": current_user,
            "error": "Return Reason is required.", "sale": sale,
        })
    if not condition_on_return or not condition_on_return.strip():
        return await _return_form_response(db, {
            "request": request, "current_user": current_user,
            "error": "Condition on Return is required.", "sale": sale,
        })
    received_by = await _valid_received_by(db, return_received_by)
    if not received_by:
        return await _return_form_response(db, {
            "request": request, "current_user": current_user,
            "error": "Return Received By is required.", "sale": sale,
        })

    # Replace Now — mark a Ready to Sale device sold at ₹0 as a replacement
    # for the returned tag, using the same customer details as the original
    # sale. Validated (and re-validated, never trusting the client) here,
    # before anything is created, so a bad replacement tag never leaves a
    # half-processed Return behind.
    do_replace = (replace_now or "").strip().lower() == "yes"
    replacement_device = None
    if do_replace:
        rtag = (replace_tag or "").strip()
        if not rtag:
            return await _return_form_response(db, {
                "request": request, "current_user": current_user,
                "error": "Select a Tag Number to replace with, or set Replace Now to No.",
                "sale": sale,
            })
        if rtag == barcode:
            return await _return_form_response(db, {
                "request": request, "current_user": current_user,
                "error": "Replacement Tag Number cannot be the same as the returned Tag Number.",
                "sale": sale,
            })
        replacement_device = (await db.execute(
            select(Device).where(func.upper(Device.barcode) == rtag.upper())
        )).scalars().first()
        if not replacement_device:
            return await _return_form_response(db, {
                "request": request, "current_user": current_user,
                "error": f"Replacement device {rtag} not found.", "sale": sale,
            })
        if replacement_device.current_stage != DeviceStage.ready_to_sale:
            return await _return_form_response(db, {
                "request": request, "current_user": current_user,
                "error": (f"Replacement device {rtag} is not in Ready to Sale stage "
                          f"(currently {STAGE_LABELS.get(replacement_device.current_stage, replacement_device.current_stage)})."),
                "sale": sale,
            })

    # Return Received By shows as Sales Person on the /sales rows this return
    # creates: the placeholder sale above (only when it was just created) and
    # the Replace Now sale below. A tag's real original sale is never changed.
    if placeholder_created:
        sale.sales_person = received_by
    # Determine intended re-entry stage (used once approved)
    if action_taken == "scrap":
        reentered_stage = "scrapped"
    else:
        reentered_stage = "iqc"

    # Server-computed warranty status at RMA time — never trust client input
    rtype = return_type if return_type in ("customer", "dealer") else "customer"
    warranty_status = warranty_status_for_sale(sale)

    # Create return as PENDING — device stage unchanged until manager approves
    ret = Return(
        sale_id=sale.id, device_id=device.id,
        reason=reason or None, condition_on_return=condition_on_return or None,
        action_taken=action_taken or None,
        reentered_stage=reentered_stage,
        processed_by=current_user.username,
        refund_amount=Decimal(refund_amount) if refund_amount else None,
        notes=notes or None,
        approval_status='pending',
        return_type=rtype,
        serial_captured=barcode or None,
        warranty_status=warranty_status,
        complaint_text=complaint_text or None,
        # cn_stage intentionally left unset here — an Internal Tags return
        # does NOT enter the Credit Note pipeline on its own (changed
        # 2026-09-23; it used to, unconditionally, which put every plain
        # repair/replace return on the Credit Note page whether or not a
        # credit note was ever intended for it). Only submitting the Return
        # New page's own Credit Note tab (process_credit_note below) stamps
        # cn_stage — see that function's docstring.
    )
    db.add(ret)

    # Item 6: mark the device as returned (Device Profile "Return Status" → Yes)
    device.return_status = True
    # Tag Return Status (2026-09-22): set immediately at submission, same as
    # return_status above — not gated on manager approval. Replace Now wins
    # over the generic "sent for repair" status when both could apply.
    device.tag_return_status = f"Replaced by {replace_tag.strip()}" if do_replace else "Return for Repair"

    # Device stage: move to Stock In immediately at submission (2026-09-24) —
    # not gated on manager approval like reentered_stage above. A returned
    # tag physically comes back into the warehouse the moment it's logged
    # here, so it showing as still "Sold" on Inventory Manager's Return
    # Stock table (device.current_stage untouched until approve_return)
    # misrepresented where the tag actually is. Approval still moves it
    # further, into IQC or Scrapped (reentered_stage, approve_return above) —
    # this only fixes the immediate landing stage, not the approval gate.
    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement)
        .where(StageMovement.device_id == device.id,
               StageMovement.to_stage  == prev_stage,
               StageMovement.exited_at == None)
        .order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()
    device.current_stage = DeviceStage.stock_in
    device.updated_at = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.stock_in,
                         moved_by=current_user.username,
                         notes="Internal Tag return submitted — back in Stock In pending approval"))
    await ensure_stage_location(db, device, DeviceStage.stock_in, current_user)

    await audit(db, user=current_user, action="RETURN_SUBMITTED",
                table_name="returns", record_id=str(device.id),
                new_value={"sale": sale.sale_number, "reason": reason,
                           "action": action_taken, "approval_status": "pending",
                           "return_type": rtype, "warranty_status": warranty_status,
                           "complaint_text": complaint_text or None,
                           "return_received_by": received_by},
                request=request)

    if do_replace and replacement_device is not None:
        sale_num = await _next_sale_number(db)
        replacement_sale = Sale(
            sale_number=sale_num, device_id=replacement_device.id, sale_price=Decimal("0"),
            customer_name=sale.customer_name, customer_phone=sale.customer_phone,
            customer_address=sale.customer_address, sold_by=current_user.username,
            sold_at=app_now(), warranty_type="none", sales_person=received_by,
            notes=f"Replacement for returned tag {barcode}",
        )
        db.add(replacement_sale)
        prev_stage = replacement_device.current_stage
        replacement_device.current_stage = DeviceStage.sold
        replacement_device.updated_at = app_now()
        # Reuses the existing Device.replaced field (no new schema needed) —
        # same field routers/stock.py and routers/repair.py already use for
        # "Replaced by/from <tag>"; "Replaced with " here is this page's own
        # prefix and is what the Returns list badge matches on.
        replacement_device.replaced = f"Replaced with {barcode}"
        db.add(StageMovement(device_id=replacement_device.id, from_stage=prev_stage,
                             to_stage=DeviceStage.sold, moved_by=current_user.username,
                             notes=f"Replacement sale for returned tag {barcode} — {sale_num}"))
        await audit(db, user=current_user, action="RETURN_REPLACEMENT_SOLD",
                    table_name="devices", record_id=str(replacement_device.id),
                    new_value={"replaced_with": barcode, "sale_number": sale_num},
                    request=request)

    await db.commit()
    return RedirectResponse(url="/returns?success=Return+submitted+for+manager+approval",
                            status_code=302)


_EXTERNAL_RETURNS_LOT_NUMBER = "EXT-RETURNS"


async def _get_or_create_external_returns_lot(db: AsyncSession) -> Lot:
    """A dedicated, clearly-labelled Lot for devices created via the
    External Tag return form, so they never get mixed into a real sourcing
    lot's records. Lot.lot_number/supplier_name are its only required
    fields."""
    lot = (await db.execute(
        select(Lot).where(Lot.lot_number == _EXTERNAL_RETURNS_LOT_NUMBER)
    )).scalar_one_or_none()
    if not lot:
        # buying_price/qty/purchase_date are NOT NULL on Lot even though they
        # mean nothing for a lot that only exists to hold external-return
        # tags — 0/0/now() rather than leaving them unset.
        lot = Lot(lot_number=_EXTERNAL_RETURNS_LOT_NUMBER, supplier_name="External / Walk-in Returns",
                  buying_price=Decimal("0"), qty=0, purchase_date=app_now())
        db.add(lot)
        await db.flush()
    return lot


@router.post("/returns/new/external")
async def process_external_return(
    request: Request,
    entity: str = Form(""),
    customer_name: str = Form(""),
    customer_phone: str = Form(""),
    customer_email: str = Form(""),
    customer_address: str = Form(""),
    reason: str = Form(""),
    condition_on_return: str = Form(""),
    tag_number: str = Form(...),
    serial_number: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    quantity: str = Form("1"),
    paid_repair: str = Form("0"),
    notes: str = Form(""),
    return_received_by: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("returns", "add")),
):
    """"External Tag" tab of Product Return New — a walk-in/external item
    with no prior Sale in this system (unlike the "Internal Tags" tab's
    process_return, which requires one). Return.sale_id is NOT NULL, so a
    placeholder Sale (sale_price=0, clearly noted) is created rather than
    loosening that constraint — keeps /returns' existing list/approval
    queries, both inner-joined to Sale, working unchanged for these too.

    Reuses (or creates) the Device by Tag Number and sets return_status=True
    on it, which is the ONLY thing Inventory Manager's Return Stock table
    (routers/stock.py stock_in_list) requires — so this submission surfaces
    there automatically, same as any Internal return once approved.
    """
    tag_number = tag_number.strip()
    if not tag_number:
        return await _return_form_response(db, {
            "request": request, "current_user": current_user, "sale": None,
            "active_tab": "external", "error": "Tag Number is required.",
        })
    if not reason.strip() or not condition_on_return.strip():
        return await _return_form_response(db, {
            "request": request, "current_user": current_user, "sale": None,
            "active_tab": "external", "error": "Return Reason and Condition on Return are required.",
        })
    received_by = await _valid_received_by(db, return_received_by)
    if not received_by:
        return await _return_form_response(db, {
            "request": request, "current_user": current_user, "sale": None,
            "active_tab": "external", "error": "Return Received By is required.",
        })

    device = (await db.execute(select(Device).where(Device.barcode == tag_number))).scalar_one_or_none()
    if not device:
        lot = await _get_or_create_external_returns_lot(db)
        device = Device(
            barcode=tag_number, lot_id=lot.id, brand=make.strip() or None, model=model.strip() or None,
            entity=entity.strip() or None, current_stage=DeviceStage.iqc, is_active=True,
        )
        db.add(device)
        await db.flush()
        await ensure_stage_location(db, device, DeviceStage.iqc, current_user)

    try:
        qty = max(1, int(quantity))
    except (TypeError, ValueError):
        qty = 1
    try:
        paid_repair_amount = Decimal(paid_repair) if paid_repair else Decimal("0")
    except Exception:
        paid_repair_amount = Decimal("0")

    sale_num = await _next_sale_number(db)
    placeholder_sale = Sale(
        sale_number=sale_num, device_id=device.id, sale_price=Decimal("0"),
        customer_name=customer_name.strip() or None, customer_phone=customer_phone.strip() or None,
        customer_address=customer_address.strip() or None, payment_mode=None,
        sold_by=current_user.username, sold_at=app_now(), warranty_type="none",
        sales_person=received_by,
        notes="Placeholder sale — External Tag return, no prior sale in system",
    )
    db.add(placeholder_sale)
    await db.flush()

    ret = Return(
        sale_id=placeholder_sale.id, device_id=device.id,
        reason=reason.strip() or None, condition_on_return=condition_on_return.strip() or None,
        action_taken="restock", reentered_stage="iqc",
        processed_by=current_user.username,
        refund_amount=paid_repair_amount,
        notes=(f"Qty: {qty}. {notes.strip()}" if notes.strip() else f"Qty: {qty}"),
        approval_status="pending", return_type="customer",
        serial_captured=serial_number.strip() or None,
        warranty_status="no_warranty",
        customer_email=customer_email.strip() or None,
    )
    db.add(ret)
    device.return_status = True

    await audit(db, user=current_user, action="EXTERNAL_RETURN_SUBMITTED",
                table_name="returns", record_id=str(device.id),
                new_value={"tag_number": tag_number, "reason": reason, "quantity": qty,
                           "paid_repair": str(paid_repair_amount), "approval_status": "pending",
                           "return_received_by": received_by},
                request=request)
    await db.commit()
    return RedirectResponse(url="/returns?success=External+return+submitted+for+manager+approval",
                            status_code=302)


@router.get("/returns/new/credit-note/lookup")
async def credit_note_lookup(barcode: str, db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(allowed)):
    """Credit Note tab's multi-tag search/add — looks up a scanned Tag
    Number by Device only. No longer requires an existing Return row (i.e.
    no longer requires the tag to have come through an Internal Tag return
    at some point — changed 2026-09-24: Credit Note is its own independent
    workflow, not something that should be gated on Internal Tags). If this
    device has no Return on file yet, process_credit_note (below) creates
    one at submit time — same placeholder-Sale pattern the External Tag tab
    already uses for a tag with no prior Sale in the system. This lookup
    stays read-only either way.

    Still not gated on cn_stage already being set (2026-09-23) — entering
    the Credit Note pipeline is this tab's own job, done on submit, not
    something Internal Tags does on its own."""
    from fastapi.responses import JSONResponse
    bc = (barcode or "").strip()
    if not bc:
        return JSONResponse({"found": False})
    device = (await db.execute(
        select(Device).where(func.upper(Device.barcode) == bc.upper())
    )).scalars().first()
    if not device:
        return JSONResponse({"found": False, "error": f"Device {bc} not found"})
    ret = (await db.execute(
        select(Return).where(Return.device_id == device.id)
        .order_by(Return.return_date.desc()).limit(1)
    )).scalars().first()
    return JSONResponse({
        "found": True, "barcode": device.barcode,
        "model": f"{device.brand or ''} {device.model or ''}".strip() or "—",
        "cn_stage": (ret.cn_stage if ret else None) or "Not yet in Credit Note pipeline",
    })


@router.post("/returns/new/credit-note")
async def process_credit_note(
    request: Request,
    barcode: list[str] = Form(...),
    debit_note_number: str = Form(...),
    debit_note_amount: str = Form(""),
    sender_name: str = Form(""),
    sender_phone: str = Form(""),
    sender_email: str = Form(""),
    return_received_by: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("returns", "add")),
):
    """Return New page's "Credit Note" tab — the ONLY place a Return row
    enters the Credit Note pipeline (changed 2026-09-23; Internal Tags used
    to do this unconditionally on every return — see process_return's
    comment). Bulk-stamps Debit Note Number/Amount and Sender Details onto
    every selected tag's existing Return record (created when the tag was
    first returned via Internal Tags) and sets cn_stage to the pipeline's
    first stage if it isn't already in it — `or` rather than an
    unconditional overwrite, so re-submitting this form for a tag already
    further along (e.g. at "Verify Payment") never regresses it back to
    "Return Received". Advancing PAST the first stage is still only the
    Credit Note page's own Action column, one stage at a time (see
    credit_note_advance). Also stamps Device.tag_return_status to
    "Return for Credit Note" (2026-09-24) — the one place that value comes
    from, distinct from Internal Tags' own "Return for Repair" / "Replaced
    by <tag>" (process_return above)."""
    codes = [b.strip() for b in barcode if b and b.strip()]
    dn = debit_note_number.strip()
    if not codes or not dn:
        return RedirectResponse(
            url="/returns/new?active_tab=credit&error=Select+at+least+one+Tag+Number+and+enter+a+Debit+Note+Number",
            status_code=302)
    received_by = await _valid_received_by(db, return_received_by)
    if not received_by:
        return RedirectResponse(
            url="/returns/new?active_tab=credit&error=Return+Received+By+is+required", status_code=302)
    try:
        amount = Decimal(debit_note_amount) if debit_note_amount.strip() else None
    except Exception:
        return RedirectResponse(
            url="/returns/new?active_tab=credit&error=Invalid+Debit+Note+Amount", status_code=302)

    devices = (await db.execute(
        select(Device).where(func.upper(Device.barcode).in_([c.upper() for c in codes]))
    )).scalars().all()
    updated = 0
    for device in devices:
        ret = (await db.execute(
            select(Return).where(Return.device_id == device.id)
            .order_by(Return.return_date.desc()).limit(1)
        )).scalars().first()
        if not ret:
            # No Internal Tag return on file for this device — Credit Note
            # is independent of that flow (2026-09-24), so create the
            # underlying Return record here instead of skipping the tag.
            # Return.sale_id is NOT NULL, so a placeholder Sale is created
            # too — same pattern as the External Tag tab (process_external_
            # return above) for a device with no prior Sale in the system.
            sale_num = await _next_sale_number(db)
            placeholder_sale = Sale(
                sale_number=sale_num, device_id=device.id, sale_price=Decimal("0"),
                sold_by=current_user.username, sold_at=app_now(), warranty_type="none",
                sales_person=received_by,
                notes="Placeholder sale — Credit Note tag with no prior Internal Tag return on file",
            )
            db.add(placeholder_sale)
            await db.flush()
            ret = Return(
                sale_id=placeholder_sale.id, device_id=device.id,
                processed_by=current_user.username,
                serial_captured=device.barcode,
                notes="Auto-created for Credit Note — no prior Internal Tag return on file.",
            )
            db.add(ret)
            await db.flush()
            device.return_status = True
        ret.cn_stage = ret.cn_stage or CN_STAGES[0]
        ret.debit_note_number = dn
        ret.debit_note_amount = amount
        ret.customer_name = sender_name.strip() or None
        ret.customer_phone = sender_phone.strip() or None
        ret.customer_email = sender_email.strip() or None
        device.tag_return_status = "Return for Credit Note"
        updated += 1

    await audit(db, user=current_user, action="CREDIT_NOTE_DEBIT_NOTE_SET",
                table_name="returns", record_id=",".join(codes),
                new_value={"barcodes": codes, "debit_note_number": dn,
                           "debit_note_amount": str(amount) if amount is not None else None,
                           "sender_name": sender_name.strip() or None,
                           "return_received_by": received_by,
                           "tags_updated": updated},
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/returns/new?active_tab=credit&success={updated}+tag(s)+updated", status_code=302)


# ── Credit Note page: every Internal Tag return, tracked through CN_STAGES ────

async def _credit_note_query_base(db: AsyncSession):
    """Every Return row currently in the Credit Note pipeline, joined for
    display — shared by the page's row query and its filter-dropdown /
    tile-count option builders so they never see a different row set."""
    return (
        select(Return, Device, Lot.lot_number, Sale.id, Sale.sale_number, Sale.sold_at)
        .join(Device, Return.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .join(Sale, Return.sale_id == Sale.id, isouter=True)
        .where(Return.cn_stage.isnot(None))
    )


@router.get("/credit-note", response_class=HTMLResponse)
async def credit_note_page(
    request: Request,
    q: str = "", lot_number: str = "", sub_lot_number: str = "", tag_return_status: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("credit_note")),
):
    base = await _credit_note_query_base(db)
    all_rows = (await db.execute(base.order_by(Return.return_date.desc()))).all()

    # Tile counts and filter-dropdown options are built from the full,
    # unfiltered pipeline — they describe the whole workflow, not just
    # whatever the current filter happens to show.
    stage_counts = {s: 0 for s in CN_STAGES}
    lot_number_options, sub_lot_number_options, tag_return_status_options = set(), set(), set()
    for ret, device, lot_num, sale_id, sale_num, sold_at in all_rows:
        if ret.cn_stage in stage_counts:
            stage_counts[ret.cn_stage] += 1
        if lot_num:
            lot_number_options.add(lot_num)
        if device.sub_lot_number:
            sub_lot_number_options.add(device.sub_lot_number)
        if device.tag_return_status:
            tag_return_status_options.add(device.tag_return_status)

    rows = all_rows
    if q:
        ql = q.strip().lower()
        rows = [r for r in rows if
                ql in (r[1].barcode or "").lower() or ql in (r[4] or "").lower()
                or ql in (r[0].payment_invoice or "").lower()]
    if lot_number:
        rows = [r for r in rows if r[2] == lot_number]
    if sub_lot_number:
        rows = [r for r in rows if r[1].sub_lot_number == sub_lot_number]
    if tag_return_status:
        rows = [r for r in rows if r[1].tag_return_status == tag_return_status]

    credit_note_rows = []
    for ret, device, lot_num, sale_id, sale_num, sold_at in rows:
        idx = CN_STAGES.index(ret.cn_stage) if ret.cn_stage in CN_STAGES else -1
        has_next = 0 <= idx < len(CN_STAGES) - 1
        credit_note_rows.append({
            "return_id": str(ret.id), "device": device, "ret": ret, "lot_number": lot_num,
            "sale_id": str(sale_id) if sale_id else None, "sale_number": sale_num, "sale_date": sold_at,
            "next_stage": CN_STAGES[idx + 1] if has_next else None,
            "action_label": CN_STAGE_ACTION_LABELS.get(idx, CN_STAGES[idx + 1] if has_next else None),
        })

    return templates.TemplateResponse("sales/credit_note.html", {
        "request": request, "current_user": current_user,
        "credit_note_rows": credit_note_rows,
        "stage_counts": stage_counts, "cn_stages": CN_STAGES,
        "lot_number_options": sorted(lot_number_options),
        "sub_lot_number_options": sorted(sub_lot_number_options),
        "tag_return_status_options": sorted(tag_return_status_options),
        "q": q, "f_lot_number": lot_number, "f_sub_lot_number": sub_lot_number,
        "f_tag_return_status": tag_return_status,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


def _try_advance_return(ret: Return, payment_invoice: str = "") -> str | None:
    """Advance one Return to its next CN_STAGES entry, in place. Returns
    None on success, or a short human-readable reason it couldn't advance.
    Shared by the single-row Action-column advance and Bulk Verify below,
    so a bulk run can report exactly which rows it skipped and why instead
    of silently dropping them."""
    if ret.cn_stage not in CN_STAGES:
        return "not in Credit Note pipeline"
    idx = CN_STAGES.index(ret.cn_stage)
    if idx >= len(CN_STAGES) - 1:
        return "already at final stage"
    next_stage = CN_STAGES[idx + 1]
    if next_stage == "Debit Note Verified" and not ret.debit_note_number:
        return "Debit Note Number not set (Return New page's Credit Note tab)"
    if next_stage == "Payment Invoice Done":
        pi = payment_invoice.strip()
        if not pi:
            return "Payment Invoice required for this step"
        ret.payment_invoice = pi
    ret.cn_stage = next_stage
    return None


async def _reset_device_return_flags_if_orphaned(db: AsyncSession, device_id) -> None:
    """After deleting a Return row, clear Device.return_status/
    tag_return_status IF this was the device's last remaining Return —
    otherwise those flags describe a record that no longer exists (the
    device would keep showing "Return for Credit Note" on its profile and
    the Production Manager "Tags Returned" tile forever). A device can carry
    more than one Return over its life (return → resold → returned again),
    so only clear when none are left."""
    remaining = (await db.execute(
        select(Return.id).where(Return.device_id == device_id).limit(1)
    )).scalar_one_or_none()
    if remaining:
        return
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if device:
        device.return_status = False
        device.tag_return_status = None


@router.post("/credit-note/{return_id}/advance")
async def credit_note_advance(
    request: Request,
    return_id: str,
    payment_invoice: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("credit_note", "edit")),
):
    """Credit Note page's Action column — moves one record to the next
    CN_STAGES entry. The Payment Invoice Done transition specifically
    requires a Payment Invoice value, since that's the one stage with its
    own stored field; every other transition is a plain advance."""
    try:
        rid = _uuid.UUID(return_id)
    except ValueError:
        raise HTTPException(404)
    ret = (await db.execute(select(Return).where(Return.id == rid))).scalar_one_or_none()
    if not ret:
        return RedirectResponse(url="/credit-note?error=Credit+Note+record+not+found", status_code=302)
    reason = _try_advance_return(ret, payment_invoice)
    if reason:
        return RedirectResponse(url=f"/credit-note?error={quote_plus(reason.capitalize())}", status_code=302)
    next_stage = ret.cn_stage
    await audit(db, user=current_user, action="CREDIT_NOTE_STAGE_ADVANCED",
                table_name="returns", record_id=str(ret.id),
                new_value={"cn_stage": next_stage}, request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/credit-note?success=Marked+{next_stage.replace(' ', '+')}", status_code=302)


@router.post("/credit-note/{return_id}/delete")
async def credit_note_delete(
    request: Request,
    return_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("credit_note", "edit")),
):
    """Credit Note page's per-row Delete — permanently removes the Return
    record (per explicit instruction: a hard delete, not just exiting the
    Credit Note pipeline). Also drops it out of Inventory Manager's Return
    Stock table (that table inner-joins Return, so it can't show a device
    with none left) and, if no other Return remains for the device, clears
    the now-stale return_status/tag_return_status flags."""
    try:
        rid = _uuid.UUID(return_id)
    except ValueError:
        raise HTTPException(404)
    ret = (await db.execute(select(Return).where(Return.id == rid))).scalar_one_or_none()
    if not ret:
        return RedirectResponse(url="/credit-note?error=Credit+Note+record+not+found", status_code=302)
    device_id, barcode = ret.device_id, ret.serial_captured
    await audit(db, user=current_user, action="CREDIT_NOTE_RETURN_DELETED",
                table_name="returns", record_id=str(ret.id),
                old_value={"cn_stage": ret.cn_stage, "debit_note_number": ret.debit_note_number,
                           "barcode": barcode},
                request=request)
    await db.delete(ret)
    await db.flush()
    await _reset_device_return_flags_if_orphaned(db, device_id)
    await db.commit()
    return RedirectResponse(url="/credit-note?success=Return+record+deleted", status_code=302)


@router.post("/credit-note/bulk-delete")
async def credit_note_bulk_delete(
    request: Request,
    return_id: list[str] = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("credit_note", "edit")),
):
    """Credit Note page's Bulk Delete — same permanent-delete semantics as
    the single-row action above, applied to every checked row."""
    ids = []
    for r in return_id:
        try:
            ids.append(_uuid.UUID(r))
        except ValueError:
            continue
    if not ids:
        return RedirectResponse(url="/credit-note?error=Select+at+least+one+row", status_code=302)
    rets = (await db.execute(select(Return).where(Return.id.in_(ids)))).scalars().all()
    device_ids = {r.device_id for r in rets}
    for ret in rets:
        await audit(db, user=current_user, action="CREDIT_NOTE_RETURN_DELETED",
                    table_name="returns", record_id=str(ret.id),
                    old_value={"cn_stage": ret.cn_stage, "debit_note_number": ret.debit_note_number,
                               "barcode": ret.serial_captured},
                    request=request)
        await db.delete(ret)
    await db.flush()
    for device_id in device_ids:
        await _reset_device_return_flags_if_orphaned(db, device_id)
    await db.commit()
    return RedirectResponse(
        url=f"/credit-note?success={len(rets)}+return+record(s)+deleted", status_code=302)


@router.post("/credit-note/bulk-verify")
async def credit_note_bulk_verify(
    request: Request,
    return_id: list[str] = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("credit_note", "edit")),
):
    """Credit Note page's Bulk Verify — advances every checked row from ITS
    OWN current cn_stage to the next one (not a single target stage for the
    whole batch — each row's Status column already differs). Rows blocked
    by a missing Debit Note Number, a missing Payment Invoice, or already
    at the final stage are skipped (not partially applied) and counted
    separately, same validation _try_advance_return already enforces for
    the single-row action — a bulk run can't collect one Payment Invoice
    value per row, so any row whose next step is "Payment Invoice Done"
    always lands in the skipped count."""
    ids = []
    for r in return_id:
        try:
            ids.append(_uuid.UUID(r))
        except ValueError:
            continue
    if not ids:
        return RedirectResponse(url="/credit-note?error=Select+at+least+one+row", status_code=302)
    rets = (await db.execute(select(Return).where(Return.id.in_(ids)))).scalars().all()
    advanced, skipped = 0, 0
    for ret in rets:
        reason = _try_advance_return(ret)
        if reason:
            skipped += 1
            continue
        await audit(db, user=current_user, action="CREDIT_NOTE_STAGE_ADVANCED",
                    table_name="returns", record_id=str(ret.id),
                    new_value={"cn_stage": ret.cn_stage}, request=request)
        advanced += 1
    await db.commit()
    msg = f"{advanced}+advanced"
    if skipped:
        msg += f",+{skipped}+skipped+(needs+Debit+Note+or+Payment+Invoice+set+individually)"
    return RedirectResponse(url=f"/credit-note?success={msg}", status_code=302)


@router.post("/credit-note/export")
async def credit_note_export_selected(
    return_id: list[str] = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("credit_note")),
):
    """Credit Note page's Export button — CSV of exactly the checked rows,
    same columns the table shows. Shares the row-selection checkboxes with
    Bulk Verify/Delete (see credit_note.html) — a row at CN Complete has its
    checkbox disabled there, so it can't be selected for this either."""
    ids = []
    for r in return_id:
        try:
            ids.append(_uuid.UUID(r))
        except ValueError:
            continue
    if not ids:
        return RedirectResponse(url="/credit-note?error=Select+at+least+one+row", status_code=302)

    base = await _credit_note_query_base(db)
    rows = (await db.execute(
        base.where(Return.id.in_(ids)).order_by(Return.return_date.desc())
    )).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Tag Number", "Tag Return Status", "Lot Number", "Sub-Lot Number", "Device Price",
                "Sale Date", "Debit Amount", "Debit Number", "Sale Number", "Payment Invoice",
                "Sender Name", "Sender Phone", "Sender Email", "Status"])
    for ret, device, lot_num, sale_id, sale_num, sold_at in rows:
        w.writerow([
            device.barcode, device.tag_return_status or "", lot_num or "", device.sub_lot_number or "",
            f"{device.device_price:.2f}" if device.device_price is not None else "",
            sold_at.strftime("%d-%m-%Y") if sold_at else "",
            f"{ret.debit_note_amount:,.0f}" if ret.debit_note_amount is not None else "",
            ret.debit_note_number or "", sale_num or "", ret.payment_invoice or "",
            ret.customer_name or "", ret.customer_phone or "", ret.customer_email or "",
            ret.cn_stage,
        ])
    buf.seek(0)
    return StreamingResponse(io.BytesIO(buf.getvalue().encode()), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=credit_note_selected.csv"})


# ── Manager: pending returns list ─────────────────────────────────────────────

MANAGER_ROLES = (UserRole.admin, UserRole.sales_manager)


@router.get("/returns/pending", response_class=HTMLResponse)
async def pending_returns(request: Request, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    if current_user.role not in MANAGER_ROLES:
        return RedirectResponse(url="/returns?error=Access+denied", status_code=302)
    result = await db.execute(
        select(Return, Device.barcode, Device.brand, Device.model,
               Sale.sale_price, Sale.sale_number)
        .join(Device, Return.device_id == Device.id)
        .join(Sale, Return.sale_id == Sale.id)
        .where(Return.approval_status == 'pending')
        .order_by(Return.return_date.desc())
    )
    pending = result.all()
    return templates.TemplateResponse("sales/returns_pending.html", {
        "request": request, "pending": pending, "current_user": current_user,
    })


# ── Manager: approve return ───────────────────────────────────────────────────

@router.post("/returns/{return_id}/approve")
async def approve_return(
    request: Request,
    return_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in MANAGER_ROLES:
        return RedirectResponse(url="/returns/pending?error=Access+denied", status_code=302)

    ret_result = await db.execute(select(Return).where(Return.id == return_id))
    ret = ret_result.scalar_one_or_none()
    if not ret:
        return RedirectResponse(url="/returns/pending?error=Return+not+found", status_code=302)
    if ret.approval_status != 'pending':
        return RedirectResponse(
            url=f"/returns/pending?error=Return+already+{ret.approval_status}", status_code=302
        )

    # Move device to intended stage
    dev_result = await db.execute(select(Device).where(Device.id == ret.device_id))
    device = dev_result.scalar_one_or_none()
    if device:
        if ret.reentered_stage == "scrapped":
            to_stage = DeviceStage.scrapped
        else:
            to_stage = DeviceStage.iqc

        prev = device.current_stage
        prev_mv = (await db.execute(
            select(StageMovement)
            .where(StageMovement.device_id == device.id,
                   StageMovement.to_stage  == prev,
                   StageMovement.exited_at == None)
            .order_by(StageMovement.moved_at.desc())
        )).scalars().first()
        if prev_mv:
            prev_mv.exited_at = app_now()

        device.current_stage = to_stage
        device.updated_at    = app_now()
        db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=to_stage,
                             moved_by=current_user.username,
                             notes=f"Return approved ({ret.action_taken}): {ret.reason}"))

    ret.approval_status = 'approved'
    ret.approved_by     = current_user.username
    ret.approved_at     = app_now()

    await audit(db, user=current_user, action="RETURN_APPROVED",
                table_name="returns", record_id=str(ret.id),
                new_value={"approved_by": current_user.username,
                           "reentered_stage": ret.reentered_stage},
                request=request)
    await db.commit()
    return RedirectResponse(url="/returns/pending?success=Return+approved", status_code=302)


# ── Manager: reject return ────────────────────────────────────────────────────

@router.post("/returns/{return_id}/reject")
async def reject_return(
    request: Request,
    return_id: str,
    rejection_reason: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in MANAGER_ROLES:
        return RedirectResponse(url="/returns/pending?error=Access+denied", status_code=302)

    ret_result = await db.execute(select(Return).where(Return.id == return_id))
    ret = ret_result.scalar_one_or_none()
    if not ret:
        return RedirectResponse(url="/returns/pending?error=Return+not+found", status_code=302)
    if ret.approval_status != 'pending':
        return RedirectResponse(
            url=f"/returns/pending?error=Return+already+{ret.approval_status}", status_code=302
        )

    ret.approval_status  = 'rejected'
    ret.approved_by      = current_user.username
    ret.approved_at      = app_now()
    ret.rejection_reason = rejection_reason or None
    # Device stays sold — no stage change on rejection

    await audit(db, user=current_user, action="RETURN_REJECTED",
                table_name="returns", record_id=str(ret.id),
                new_value={"rejected_by": current_user.username,
                           "reason": rejection_reason},
                request=request)
    await db.commit()
    return RedirectResponse(url="/returns/pending?success=Return+rejected", status_code=302)
