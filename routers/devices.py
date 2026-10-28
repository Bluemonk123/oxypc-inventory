"""
Device Management — Search, Detail, Edit
Provides a global inventory browser and per-device history view.
"""
from services.parts_changed import changed_parts_by_device, parts_badge_map, parts_display_map, spec_cell
from templates_config import templates
import csv, io, re, uuid as uuid_module
from datetime import datetime
from utils.timezone import app_now
from utils.csv_decode import decode_csv_bytes
from fastapi import APIRouter, Depends, Form, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func, and_, update, case, cast, String, exists, not_
from utils.touch_type import touch_badge_html
from utils.flag import flag_badge_html
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceGrade, DeviceStage, StageMovement, STAGE_LABELS, DROPDOWN_STAGES
from models.master import EXTERNAL_PARTNER_TEST_ENTITY
from models.lot import Lot
from models.repair import RepairJob, RepairStatus
from models.qc import QCCheck
from models.spare_parts import SparePartConsumption, SparePart
from models.location import DeviceLocationLog, StorageLocation, LocationAction, UNIT_TYPE_LABELS, ZONE_LABELS
from models.iqc_inspection import IQCInspection
from models.part_request import PartRequest
from models.pna_part import DevicePNAPart
from models.work_order import WorkOrder
from models.engines import DeviceCosting
from models.sales import Sale
from services.parts_required import compute_required, LEGACY_LABELS
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_additional_perm
from utils.warranty import effective_warranty_for_sale, warranty_status_for_sale
from utils.master_data import master_values, entity_values

from utils.device_spec import gb_text_spaced as _gb_text  # "16 GB", whole TB as "1 TB" (2026-10-28)
from services.work_order_close import wo_open  # open = not completed / reassigned

router = APIRouter(prefix="/devices", tags=["devices"], dependencies=[Depends(verify_csrf)])
# All logged-in users can search/view; only admin+invmgr can edit
view_allowed = get_current_user
edit_allowed = require_roles(UserRole.admin, UserRole.inventory_manager)


@router.get("/api/brief")
async def device_brief(barcode: str, require_stage: str = "", db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(view_allowed)):
    """Brief device info for live tag lookups (Process Return + L3 'Replace Device
    with' + Product Return 'Replace Now' + Extended Warranty). Returns
    Make/Model/CPU/Lot/RAM/Storage/Location/Status/Warranty as JSON.

    `require_stage`, when passed (e.g. "ready_to_sale"), restricts the match to a
    device currently in that stage and reports found:false with an explanatory
    error otherwise — used by Replace Now, which must only ever offer a device
    that's actually sellable right now."""
    from fastapi.responses import JSONResponse
    bc = (barcode or "").strip()
    if not bc:
        return JSONResponse({"found": False})
    # .scalars().first(), not scalar_one_or_none(): this codebase's barcodes
    # are inconsistently cased, and if a legacy duplicate-case row exists,
    # scalar_one_or_none() raises MultipleResultsFound — an unhandled 500
    # that the caller's fetch().then(r => r.ok ? ... : {found:false}) turns
    # into a false "not found" for a tag that plainly exists.
    # Ordering (2026-10-17): with duplicate-case rows (e.g. "...L1840" sold
    # and a trashed "...l1840" in L1/L2), .first() used to return whichever
    # row Postgres happened to read first — Process Return showed the
    # trashed copy's stage. Prefer a live (not trashed, active) row, then an
    # exact-case Tag Number, then a Tag Number match over a Serial No match,
    # then the most recently updated.
    device = (await db.execute(
        select(Device).where(or_(Device.barcode.ilike(bc), Device.serial_no.ilike(bc)))
        .order_by(
            Device.is_trashed.asc(),
            Device.is_active.desc(),
            (Device.barcode == bc).desc(),
            Device.barcode.ilike(bc).desc(),
            Device.updated_at.desc().nullslast(),
        )
    )).scalars().first()
    if not device:
        return JSONResponse({"found": False})
    if require_stage and device.current_stage.value != require_stage:
        return JSONResponse({
            "found": False,
            "error": (f"{device.barcode} is not in Ready to Sale stage "
                      f"(currently {STAGE_LABELS.get(device.current_stage, device.current_stage)})."),
        })
    lot = (await db.execute(select(Lot).where(Lot.id == device.lot_id))).scalar_one_or_none()
    lot_number = lot.lot_number if lot else "—"
    sale = (await db.execute(
        select(Sale).where(Sale.device_id == device.id).order_by(Sale.sold_at.desc()).limit(1)
    )).scalars().first()
    w = effective_warranty_for_sale(sale)
    loc = None
    info = (await _build_location_map(db, [str(device.id)])).get(str(device.id))
    if info and info.get("unit_id"):
        loc = info["unit_id"]
    if not loc:
        loc = device.warehouse or device.floor or "—"
    ram = _gb_text(device.ram_gb) or "—"
    if device.storage_gb:
        storage = _gb_text(device.storage_gb) + (f" {device.storage_type}" if device.storage_type else "")
    else:
        storage = "—"
    sold_grade = device.grade or "—"
    sold_on = sale.sold_at.strftime("%d-%m-%Y") if sale and sale.sold_at else "—"
    if sale and w:
        warranty_status = "Within Warranty" if w["status"] == "active" else "Out of Warranty"
    elif sale:
        warranty_status = "Out of Warranty"
    else:
        warranty_status = "Not Sold Yet"

    # ── Sale warranty_type fields (Phase 1a/1b RMA capture) ──────────────────────
    rma_warranty_type = getattr(sale, "warranty_type", None) if sale else None
    rma_warranty_expires_at = getattr(sale, "warranty_expires_at", None) if sale else None
    rma_warranty_status = warranty_status_for_sale(sale)  # in_warranty/out_of_warranty/no_warranty

    return JSONResponse({
        "found": True,
        "barcode": device.barcode,
        "make": device.brand or "—",
        "model": device.model or "—",
        "cpu": device.cpu or "—",
        "lot_number": lot_number,
        "ram": ram,
        "storage": storage,
        "location": loc,
        "status": str(device.stage_label),
        "warranty": w["label"] if w else "No warranty",
        "warranty_status": warranty_status,
        "sold_grade": sold_grade,
        "sold_on": sold_on,
        "return_status": "Yes" if device.return_status else "No",
        "rma_warranty_type": rma_warranty_type or "none",
        "rma_warranty_expires_at": rma_warranty_expires_at.strftime("%d-%m-%Y") if rma_warranty_expires_at else None,
        "rma_warranty_status": rma_warranty_status,
    })


@router.get("/api/search-tags")
async def search_tags(q: str = "", db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(view_allowed)):
    """Partial tag-number search for the Scrap Products 'Replace' modal — type
    a few characters, get up to 10 matching devices with the detail fields
    the modal shows (Stage, Tag Number, Lot Number, Make, Model, CPU, RAM,
    Hard Drive, Grade, Device Price)."""
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    rows = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where(Device.barcode.ilike(f"%{q}%"), Device.is_active == True, Device.is_trashed == False)
        .order_by(Device.updated_at.desc())
        .limit(10)
    )).all()
    results = []
    for device, lot_number in rows:
        # "8 GB" / "256 GB SSD" with the space (2026-10-27), as the Basic Specs edit saves.
        from utils.device_spec import gb_text_spaced
        ram = gb_text_spaced(device.ram_gb) or "—"
        if device.storage_gb:
            storage = gb_text_spaced(device.storage_gb) + (f" {device.storage_type}" if device.storage_type else "")
        else:
            storage = "—"
        results.append({
            "barcode": device.barcode,
            "stage": str(device.stage_label),
            "lot_number": lot_number or "—",
            "make": device.brand or "—",
            "model": device.model or "—",
            "cpu": device.cpu or "—",
            "ram": ram,
            "storage": storage,
            "grade": device.grade.value if device.grade else "—",
            "device_price": f"{device.device_price:.2f}" if device.device_price is not None else None,
        })
    return JSONResponse({"results": results})


@router.get("/api/quick-search")
async def quick_search(q: str = "", db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(view_allowed)):
    """Top-bar Search / Scan box (2026-10-24, templates/base.html +
    static/js/topbar-search.js): Tag Number or Serial Number, partial match,
    up to 10 tags (not in Trash). Exact Tag Number first, then tags starting
    with q, then active before inactive, then most recently updated."""
    from sqlalchemy import case as _case, func as _func
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    like = f"%{q}%"
    exact = _func.upper(Device.barcode) == q.upper()
    starts = Device.barcode.ilike(f"{q}%")
    rows = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where((Device.barcode.ilike(like)) | (Device.serial_no.ilike(like)),
               Device.is_trashed.isnot(True))
        .order_by(_case((exact, 0), else_=1), _case((starts, 0), else_=1),
                  Device.is_active.desc(), Device.updated_at.desc())
        .limit(10)
    )).all()
    results = []
    for device, lot_number in rows:
        if device.storage_gb:
            storage = _gb_text(device.storage_gb) + (f" {device.storage_type}" if device.storage_type else "")
        else:
            storage = "—"
        results.append({
            "barcode": device.barcode,
            "lot_number": lot_number or "—",
            "cpu": device.cpu or "—",
            "ram": _gb_text(device.ram_gb) or "—",
            "storage": storage,
            "grade": device.grade.value if device.grade else "—",
            "stage": str(device.stage_label or "—"),
            "stage_color": device.stage_color or "secondary",
            "exact": (device.barcode or "").upper() == q.upper(),
        })
    return JSONResponse({"results": results})


@router.get("/api/asset-history")
async def device_asset_history(barcode: str, db: AsyncSession = Depends(get_db),
                               current_user: User = Depends(view_allowed)):
    """Search to View Asset History modal (shared across L1/L2, L3/L4, Stress,
    All Tags, Final QC, Production Manager — templates/_search_tags_modal.html).
    Current stage + the device's most recent stage movements — same
    StageMovement query/shape Device Detail's own "Asset History" table
    already uses (routers/devices.py device_detail(), templates/devices/
    detail.html), just returned as JSON, newest first, capped to the most
    recent 20 rows for a compact modal rather than the full lifetime list."""
    bc = (barcode or "").strip()
    if not bc:
        return JSONResponse({"found": False})
    # .scalars().first(), not scalar_one_or_none(): this codebase's barcodes
    # are inconsistently cased, and if a legacy duplicate-case row exists,
    # scalar_one_or_none() raises MultipleResultsFound — an unhandled 500
    # that the caller's fetch().then(r => r.ok ? ... : {found:false}) turns
    # into a false "not found" for a tag that plainly exists.
    device = (await db.execute(
        select(Device).where(or_(Device.barcode.ilike(bc), Device.serial_no.ilike(bc)))
    )).scalars().first()
    if not device:
        return JSONResponse({"found": False})
    movements = (await db.execute(
        select(StageMovement).where(StageMovement.device_id == device.id)
        .order_by(StageMovement.moved_at.desc()).limit(20)
    )).scalars().all()
    return JSONResponse({
        "found": True,
        "barcode": device.barcode,
        "current_stage": str(device.stage_label),
        "movements": [
            {
                "from_stage": STAGE_LABELS.get(mv.from_stage, mv.from_stage.value if mv.from_stage else "—") if mv.from_stage else "—",
                "to_stage": STAGE_LABELS.get(mv.to_stage, mv.to_stage.value) if mv.to_stage else "—",
                "moved_by": mv.moved_by or "—",
                "moved_at": mv.moved_at.strftime("%d-%m-%Y %H:%M") if mv.moved_at else "—",
                "notes": mv.notes or "",
            }
            for mv in movements
        ],
    })


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _build_location_map(db: AsyncSession, device_ids: list) -> dict:
    """Return {str(device_id): {unit_id, zone, slot, action, actor_name}} for a batch of devices.

    `zone` and `slot` are carried alongside `unit_id` (rather than a separate
    map) so every existing caller that only reads `unit_id` keeps working
    unchanged, while callers that also need the bin (`slot`) or zone — the
    Zone export column and the All Inventory Bin badge — can read them off
    the same dict without a second query."""
    if not device_ids:
        return {}
    try:
        uuid_ids = [uuid_module.UUID(did) if isinstance(did, str) else did for did in device_ids]
    except (ValueError, AttributeError):
        return {}
    sub = (
        select(
            DeviceLocationLog.device_id,
            func.max(DeviceLocationLog.logged_at).label("latest"),
        )
        # Only the asked-for devices (2026-10-19) — the latest-log subquery
        # used to group the whole location log on every call.
        .where(DeviceLocationLog.device_id.in_(uuid_ids))
        .group_by(DeviceLocationLog.device_id)
        .subquery()
    )
    rows = await db.execute(
        select(
            DeviceLocationLog.device_id,
            StorageLocation.id,
            StorageLocation.unit_id,
            StorageLocation.zone,
            StorageLocation.slot,
            DeviceLocationLog.action,
            DeviceLocationLog.actor_name,
        )
        .join(sub, and_(
            DeviceLocationLog.device_id == sub.c.device_id,
            DeviceLocationLog.logged_at == sub.c.latest,
        ))
        .outerjoin(StorageLocation, DeviceLocationLog.location_id == StorageLocation.id)
        # (device filter lives in the subquery above, so the IN() list is
        # bound once — keeps a 20,000-id chunk under Postgres' 32767 cap)
    )
    loc_map = {}
    for device_id, location_id, unit_id, zone, slot, action, actor_name in rows.all():
        loc_map[str(device_id)] = {
            "location_id": location_id,
            "unit_id": unit_id,
            "zone": zone,
            "slot": slot,
            "action": action.value if action else None,
            "actor_name": actor_name,
        }
    return loc_map


async def _get_device_or_404(barcode: str, db: AsyncSession) -> Device:
    result = await db.execute(
        select(Device).where(Device.barcode == barcode)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device '{barcode}' not found")
    return device


def _iqc_date_filter(date_from, date_to):
    """Return a Device.id-scoped filter clause for devices whose IQC
    inspection date (IQCInspection.inspected_at) falls in range — NOT
    Device.created_at. Self-contained via a subquery (rather than a join)
    so every caller that reuses the shared filter list stays safe, even
    queries that never join IQCInspection themselves."""
    from utils.date_filter import parse_date_range
    start, end = parse_date_range(date_from, date_to)
    if start is None and end is None:
        return None
    q = select(IQCInspection.device_id)
    if start is not None:
        q = q.where(IQCInspection.inspected_at >= start)
    if end is not None:
        q = q.where(IQCInspection.inspected_at < end)
    return Device.id.in_(q)


def _current_stage_date_filter(date_from, date_to):
    """All Inventory's Date From/To: the date the device reached its current
    stage, i.e. the latest StageMovement into Device.current_stage (when the
    previous stage was completed). A device with no such movement on record
    falls back to its created date. Correlated subquery, so callers need no
    extra join."""
    from utils.date_filter import parse_date_range
    start, end = parse_date_range(date_from, date_to)
    if start is None and end is None:
        return None
    reached = func.coalesce(
        select(func.max(StageMovement.moved_at))
        .where(StageMovement.device_id == Device.id,
               StageMovement.to_stage == Device.current_stage)
        .correlate(Device).scalar_subquery(),
        Device.created_at,
    )
    clauses = []
    if start is not None:
        clauses.append(reached >= start)
    if end is not None:
        clauses.append(reached < end)
    return and_(*clauses)


def _exclude_sold(exclude_sold: str, fs: str) -> bool:
    """Resolve the All Inventory "Exclude Sold" checkbox.

    An unchecked HTML checkbox submits nothing, so a bare absent value is
    ambiguous: it means either "first page load" or "user unticked it". The
    form carries a hidden fs=1 marker, so absence of fs means first load →
    default the filter ON; presence of fs means trust the checkbox.
    """
    if not fs:
        return True
    return exclude_sold in ("1", "true", "on", "yes")


def _multi(value) -> list:
    """Split a filter parameter into the list of values it selects.

    The All Inventory filters are multi-select checkboxes that post one
    comma-separated value per field, rather than repeating the parameter.
    That keeps every consumer's signature a plain `str` — the page, the
    DataTables feed, the export and the barcode list all share these helpers —
    and leaves single-value URLs that people have pasted or bookmarked working
    exactly as before, since a lone value simply splits into a one-item list.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = str(value).split(",")
    return [v.strip() for v in raw if v and str(v).strip()]


def _spec_search_clause(term: str, extra_cols=()):
    """Multi-word search across Tag Number + Make/Model/CPU/RAM/Storage.

    Every word must match at least one field (AND across words, OR across
    fields), so "Dell 8GB i5" finds a Dell with 8 GB RAM and an i5 CPU, while a
    single word still works on its own. RAM/Storage are matched on their
    packed summaries ("8GB_DDR4…") and on the plain size ("8GB", "512GB",
    "1TB"), since the integer columns alone can never match "8GB". Returns
    None for a blank term.
    """
    words = [w for w in re.split(r"\s+", (term or "").strip()) if w]
    if not words:
        return None
    ram_txt = func.concat(cast(Device.ram_gb, String), "GB")
    sto_txt = func.concat(cast(Device.storage_gb, String), "GB")
    sto_tb = case((Device.storage_gb >= 1000,
                   func.concat(cast(func.round(Device.storage_gb / 1000.0), String), "TB")),
                  else_="")
    cols = [Device.barcode, Device.serial_no, Device.brand, Device.model, Device.cpu,
            Device.device_type, Device.ram_summary, Device.hdd_summary, Device.storage_type,
            Device.total_ram_size, Device.total_hdd_size,
            ram_txt, sto_txt, sto_tb, *extra_cols]
    clauses = []
    for w in words:
        like = f"%{w}%"
        clauses.append(or_(*[c.ilike(like) for c in cols]))
    return and_(*clauses)


def _device_search_filters(q, stage, lot, grade, category, device_type, date_from, date_to, entity="", exclude_sold=False,
                           stock_type="", pna=""):
    """Filter clauses shared by the Inventory Search page and its data endpoint.

    `employee` is deliberately NOT handled here — Device has no direct
    "assigned employee" column; that comes from a join to WorkOrder, which
    only the page route (not this shared helper) needs to perform.

    Date From/To filter on the date the device's current stage was reached
    (2026-10-13; was the IQC inspection date) — see _current_stage_date_filter.
    """
    w = []
    if q:
        # Search box also matches GRN Number and Invoice Number (2026-10-15).
        _spec = _spec_search_clause(q, extra_cols=(Device.grn_number, Device.invoice_number))
        if _spec is not None:
            w.append(_spec)

    stage_vals = _multi(stage)
    if stage_vals:
        stages = []
        for v in stage_vals:
            try:
                stages.append(DeviceStage(v))
            except ValueError:
                pass
        if stages:
            w.append(Device.current_stage.in_(stages))
    lot_vals = _multi(lot)
    if lot_vals:
        # Kept as ILIKE-per-value rather than IN: the single-select version
        # matched partial lot numbers and pasted URLs rely on that.
        w.append(or_(*[Lot.lot_number.ilike(f"%{v}%") for v in lot_vals]))
    grade_vals = _multi(grade)
    if grade_vals:
        # "none" = the built-in No Grade option: tags with no grade at all.
        real = [v for v in grade_vals if v != "none"]
        clauses = [Device.grade.in_(real)] if real else []
        if "none" in grade_vals:
            clauses.append(Device.grade.is_(None))
        w.append(or_(*clauses))
    category_vals = _multi(category)
    if category_vals:
        w.append(Device.sub_category.in_(category_vals))
    device_type_vals = _multi(device_type)
    if device_type_vals:
        w.append(Device.device_type.in_(device_type_vals))
    entity_vals = _multi(entity)
    if entity_vals:
        # "Unassigned" isn't a real Device.entity value — it's the summary
        # strip's label for a null one (see entity_counts below) — so the
        # clickable badge for it needs to match NULL/empty, not the literal
        # string, or it silently returns zero rows.
        real_vals = [v for v in entity_vals if v != "Unassigned"]
        clauses = []
        if real_vals:
            clauses.append(Device.entity.in_(real_vals))
        if len(real_vals) != len(entity_vals):
            clauses.append(or_(Device.entity.is_(None), Device.entity == ""))
        w.append(or_(*clauses))
    else:
        # No explicit Entity filter -> exclude the External Partner
        # test-data entity by default (models/master.py
        # EXTERNAL_PARTNER_TEST_ENTITY), same convention as
        # routers/dashboard.py. An explicit filter that includes it (the
        # `if entity_vals:` branch above) is respected as-is. NULL-safe:
        # `entity != X` alone silently drops every entity-less ("Unassigned")
        # device too, since SQL NULL != X evaluates to NULL, not TRUE.
        w.append(or_(Device.entity.is_(None), Device.entity != EXTERNAL_PARTNER_TEST_ENTITY))
    # "Active Stock Only" filter (form field name kept as exclude_sold for
    # backward compatibility with saved links/bookmarks) — on by default so
    # All Inventory shows live stock rather than the full historical device
    # list. Excludes GRN Receipt and Sold only (2026-10-19): Returned,
    # Scrapped and Scrap for Sale devices are still physically held, so they
    # now count as active stock here. (The Dashboard's "Total Inventory"
    # figures still exclude all five.) Either stage is skipped from this
    # exclusion when the user has explicitly selected it in the Stage filter,
    # which would otherwise return nothing.
    if exclude_sold:
        _active_stock_excluded = [
            s for s in (DeviceStage.grn, DeviceStage.sold)
            if s.value not in stage_vals
        ]
        if _active_stock_excluded:
            w.append(Device.current_stage.notin_(_active_stock_excluded))
    # PNA Check (2026-10-22): same rule as the export's PNA column and the
    # Production Manager PNA summary (_pna_label) — an active PNA part, in
    # L1/L2/L3, and active. "yes" = those tags, "no" = every other tag.
    pna_v = (pna or "").strip().lower()
    if pna_v in ("yes", "no"):
        from services.pna_lookup import PNA_SUMMARY_STAGES
        in_pna = and_(
            Device.current_stage.in_(PNA_SUMMARY_STAGES),
            Device.is_active == True,  # noqa: E712
            exists().where(DevicePNAPart.device_id == Device.id,
                           DevicePNAPart.is_active == True),  # noqa: E712
        )
        w.append(in_pna if pna_v == "yes" else not_(in_pna))
    # Stock Type: As-Is / Finish Good / In Progress — see stock_type_key().
    stock_vals = set(_multi(stock_type)) & set(STOCK_TYPE_LABELS)
    if stock_vals and stock_vals != set(STOCK_TYPE_LABELS):
        w.append(stock_type_sql().in_(sorted(stock_vals)))
    date_filter = _current_stage_date_filter(date_from, date_to)
    if date_filter is not None:
        w.append(date_filter)
    return w


async def _employee_device_id_filter(db: AsyncSession, employee: str):
    """Return a Device.id-scoped filter clause for the given assigned-employee
    name, via WorkOrder (Device has no direct assigned-employee column)."""
    emp_device_ids = set((await db.execute(
        select(WorkOrder.device_id)
        .where(WorkOrder.assigned_name == employee, wo_open())
    )).scalars().all())
    return Device.id.in_(emp_device_ids) if emp_device_ids else Device.id.in_([])


_STAGE_BADGE = {
    "iqc": "secondary", "stock_in": "info text-dark", "l1": "warning text-dark",
    "l2": "warning text-dark", "l3": "warning text-dark", "qc_check": "primary",
    "final_qc": "primary", "ready_to_sale": "success", "sold": "dark",
    "returned": "danger", "cosmetic_received": "purple", "cleaning": "purple", "dry_sanding": "purple",
    "masking": "purple", "painting": "purple", "water_sanding": "purple", "cosmetic_completed": "purple",
}


@router.get("/data")
async def device_search_data(
    request: Request,
    draw: int = 1, start: int = 0, length: int = 25,
    q: str = "", stage: str = "", lot: str = "", grade: str = "",
    category: str = "", device_type: str = "", date_from: str = "", date_to: str = "",
    employee: str = "", entity: str = "", exclude_sold: str = "", fs: str = "", stock_type: str = "", pna: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """DataTables server-side feed for Inventory Search.

    Same reasoning as the Sales list conversion: at ~11,000 devices this table
    rendered every row into the HTML on every page load — the page would hang
    the browser for several seconds before becoming interactive, and grew
    steadily worse as devices accumulated (19,000+ by 2026-07-30). Rows now come
    a page at a time; search, sort and paging still reach every device.
    """
    from sqlalchemy import desc as _desc, asc as _asc
    from models.role_permissions import has_perm as _hp
    from html import escape

    role = getattr(current_user.role, "value", current_user.role)
    show_pricing = _hp(role, "devices", "pricing")   # Permission Matrix -> Inventory Search -> Pricing

    page_filters = _device_search_filters(q, stage, lot, grade, category, device_type, date_from, date_to,
                                     entity=entity, exclude_sold=_exclude_sold(exclude_sold, fs),
                                     stock_type=stock_type, pna=pna)
    if employee:
        page_filters.append(await _employee_device_id_filter(db, employee))
    base = select(Device, Lot.lot_number).join(Lot, Device.lot_id == Lot.id).where(Device.is_trashed == False)
    count_base = select(func.count()).select_from(Device).join(Lot, Device.lot_id == Lot.id).where(Device.is_trashed == False)

    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = []
    if search:
        _spec = _spec_search_clause(search, extra_cols=(Lot.lot_number,))
        if _spec is not None:
            search_filters.append(_spec)

    total = (await db.execute(count_base.where(*page_filters))).scalar() or 0
    # With no search term the two counts are the same query over 22k+ rows, and
    # DataTables issues one on every draw — paging, sorting, changing page size.
    # Only pay for the second when a search term actually narrows the set.
    filtered = total if not search_filters else (
        (await db.execute(count_base.where(*page_filters, *search_filters))).scalar() or 0)

    # Column 3 is Location ID (inserted after Lot) — every index from Brand
    # onward shifts by one to make room for it. Column 11 is Stock Type (after
    # Grade). Stock Price / Sale Price are no longer shown for ANY role
    # (2026-10-07), so the column layout is the same for everyone and "Updated"
    # is always column 13 — which also removes the old per-role index shift that
    # once left the table stuck on "Processing…" for roles without pricing.
    # Bin Location moved INTO the Location ID cell (2026-10-22, small text
    # below the ID) — its own column is gone, so Brand is 4 again and
    # "Updated" is 13.
    updated_col = 13
    stock_type_expr = stock_type_sql()
    col_map = {1: Device.barcode, 2: Lot.lot_number, 4: Device.brand,
               5: Device.model, 6: Device.device_type, 7: Device.cpu, 10: Device.grade,
               11: stock_type_expr, updated_col: Device.updated_at}
    try:
        order_col = int(request.query_params.get("order[0][column]", updated_col))
    except ValueError:
        order_col = updated_col
    order_dir = request.query_params.get("order[0][dir]", "desc")
    sort_expr = col_map.get(order_col, Device.updated_at)
    order_by = _asc(sort_expr) if order_dir == "asc" else _desc(sort_expr)

    rows = (await db.execute(
        base.where(*page_filters, *search_filters)
        .order_by(order_by, Device.barcode)
        .offset(max(0, start)).limit(min(max(1, length), 5000))
    )).all()

    device_ids = [d.id for d, _ in rows]

    # Location ID column — same source as Device Detail's own "Location ID"
    # row: the latest DeviceLocationLog entry (what "assign one" / the
    # pickup-placeback flow at /locations/device/{id} actually writes), with
    # device.location_id as a fallback for a device assigned only via Edit
    # Device's dropdown and never through that flow. Reading device.location_id
    # alone here previously showed nothing for the normal-path assignment.
    # Each value is (unit_id, slot) so the Bin badge below can render the
    # StorageLocation's bin (slot) alongside the Location ID without a
    # second per-row lookup.
    location_map = {}
    if device_ids:
        log_map = await _build_location_map(db, device_ids)
        location_map = {did: (v["unit_id"], v.get("slot")) for did, v in log_map.items() if v.get("unit_id")}
        missing = [d for d in device_ids if str(d) not in location_map]
        if missing:
            for did, unit_id, slot in (await db.execute(
                select(Device.id, StorageLocation.unit_id, StorageLocation.slot)
                .join(StorageLocation, Device.location_id == StorageLocation.id)
                .where(Device.id.in_(missing))
            )).all():
                location_map[str(did)] = (unit_id, slot)

    parts_badges, parts_data = await parts_display_map(db, device_ids)

    # Stage column: a Sold tag links to its sale (the latest sale, should a tag
    # have been returned and sold again) — one query for the page's sold rows.
    sale_map = {}
    sold_ids = [d.id for d, _ in rows if d.current_stage == DeviceStage.sold]
    if sold_ids:
        from models.sales import Sale
        for sid, snum, did in (await db.execute(
            select(Sale.id, Sale.sale_number, Sale.device_id)
            .where(Sale.device_id.in_(sold_ids))
            .order_by(Sale.sold_at.desc())
        )).all():
            sale_map.setdefault(did, (sid, snum))

    def esc(v):
        return escape(str(v)) if v is not None else ""

    data = []
    for d, lot_number in rows:
        g = getattr(d.grade, "value", d.grade) if d.grade else None
        gcls = ("success" if g == "A" else "warning text-dark" if g == "B" else
                "secondary" if g == "C" else "danger" if g in ("D", "scrap") else "light text-dark")
        stage_val = getattr(d.current_stage, "value", d.current_stage)
        stage_lbl = STAGE_LABELS.get(d.current_stage, stage_val)
        # Same value as the Export's RAM / Storage columns (2026-10-27): the
        # table showed RAM Summary first while Export showed Total RAM Size
        # first, so a tag whose two disagreed showed different RAM in each.
        ram = ram_text(d) or "—"
        storage = storage_text(d) or "—"
        cells = [
            f'<input type="checkbox" class="form-check-input rowChk" value="{esc(d.barcode)}">',
            (f'<span class="text-nowrap"><a href="/devices/{esc(d.barcode)}" class="font-monospace fw-bold text-decoration-none">{esc(d.barcode)}</a>'
             + touch_badge_html(d.touch_type) + flag_badge_html(d)
             + parts_badges.get(str(d.id), '') + '</span>' +
             f'<div style="color:#999999;font-size:12px;">{esc(d.entity) if d.entity else "—"}</div>'),
            # Lot Number: plain link, no outline (2026-10-22)
            (f'<a href="/devices?lot={esc(lot_number)}" class="text-decoration-none text-nowrap" '
             f'style="color:#6C757D;">{esc(lot_number)}</a>'),
            # Location ID cell (2026-10-22): Bin Location on top, a thin
            # divider, then the Location ID — plain text, no outlines; bin in
            # the standard dark text colour, Location ID in Bluemonk Blue
            # (#0377c2). Bin =
            # the tag's own Bin Location (Customise modal), else the
            # StorageLocation's slot. No location yet → Assign button.
            (lambda _bin, _loc: (
                (f'<div class="font-monospace text-nowrap" style="color:#212529;'
                 f'padding-bottom:4px;margin-bottom:4px;border-bottom:1px solid #d5dde5;">{esc(_bin)}</div>'
                 if _bin else '')
                + (f'<div class="font-monospace text-nowrap" style="font-size:12px;color:#0377c2;">{esc(_loc)}</div>'
                   if _loc else
                   f'<a href="/locations/device/{d.id}" class="btn btn-sm btn-outline-primary py-0 px-2">Assign</a>')
            ))(d.bin_location or (location_map[str(d.id)][1] if str(d.id) in location_map else None),
               location_map[str(d.id)][0] if str(d.id) in location_map else None),
            esc(d.brand or "—"), esc(d.model or "—"), esc(d.device_type or "—"), esc(d.cpu or "—"),
            spec_cell(parts_data, d.id, "RAM", esc(ram)),
            spec_cell(parts_data, d.id, "Hard Drive", esc(storage)),
            (f'<span class="badge bg-{gcls}">{esc(g)}</span>' if g else "—"),
            # Stock Type: As-Is / Finish Good / In Progress (stock_type_key)
            (lambda k: f'<span class="badge bg-{STOCK_TYPE_BADGE[k]}">{STOCK_TYPE_LABELS[k]}</span>')(
                stock_type_key(d.current_stage, d.sub_lot_number)),
        ]
        cells += [
            (f'<span class="badge bg-{_STAGE_BADGE.get(stage_val, "light text-dark")}">{esc(stage_lbl)}</span>'
             + (f'<div><a href="/sales/{sale_map[d.id][0]}" class="small font-monospace text-decoration-none text-nowrap" '
                f'title="Open sale">{esc(sale_map[d.id][1])}</a></div>' if d.id in sale_map else '')),
            d.updated_at.strftime("%d-%m-%Y") if d.updated_at else "—",
            (f'<div class="d-flex gap-1 justify-content-center">'
             f'<a href="/devices/{esc(d.barcode)}" class="btn btn-sm btn-outline-primary py-0 px-1" title="View"><i class="bi bi-eye"></i></a>'
             f'<a href="/devices/{esc(d.barcode)}/edit" class="btn btn-sm btn-outline-warning py-0 px-1" title="Edit"><i class="bi bi-pencil"></i></a>'
             f'<button type="button" class="btn btn-sm btn-outline-danger py-0 px-1 trash-one-btn" data-barcode="{esc(d.barcode)}" title="Move to Trash"><i class="bi bi-trash3"></i></button>'
             f'</div>'),
        ]
        data.append(cells)

    return {"draw": draw, "recordsTotal": total, "recordsFiltered": filtered,
            "data": data, "showPricing": show_pricing}


@router.get("/barcodes")
async def device_barcodes(
    q: str = "", stage: str = "", lot: str = "", grade: str = "",
    category: str = "", device_type: str = "", date_from: str = "", date_to: str = "",
    employee: str = "", entity: str = "", exclude_sold: str = "", fs: str = "", stock_type: str = "", pna: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Every barcode matching the current filters — powers "Select All" across
    every page and the tag-upload bulk-select feature below. Neither can rely
    on DataTables' serverSide rows() API, which only reaches the currently
    rendered page."""
    page_filters = _device_search_filters(q, stage, lot, grade, category, device_type, date_from, date_to,
                                     entity=entity, exclude_sold=_exclude_sold(exclude_sold, fs),
                                     stock_type=stock_type, pna=pna)
    if employee:
        page_filters.append(await _employee_device_id_filter(db, employee))
    barcodes = (await db.execute(
        select(Device.barcode).join(Lot, Device.lot_id == Lot.id)
        .where(Device.is_trashed == False, *page_filters)
    )).scalars().all()
    return {"barcodes": barcodes}


@router.post("/upload-tags")
async def device_upload_tags(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Bulk Upload Tags on All Inventory — reads a single-column CSV of tag
    numbers and reports which exist, so the page can tick the matching rows
    (including rows on pages never rendered) ahead of a Customise bulk action.
    Read-only: selects nothing, changes nothing."""
    content = await file.read()
    text_data = decode_csv_bytes(content)
    reader = csv.DictReader(io.StringIO(text_data))

    field_map = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    key = field_map.get("tag_number") or field_map.get("barcode")
    if not key:
        return JSONResponse(
            {"error": "CSV must have a 'tag_number' (or 'barcode') column header"},
            status_code=400,
        )

    tags, errors = [], []
    seen = set()
    rows_read = 0
    duplicates = []
    for i, row in enumerate(reader, start=2):
        rows_read += 1
        tag = (row.get(key) or "").strip()
        if not tag:
            errors.append(f"Row {i}: tag_number is empty")
            continue
        # Deduplicated case-insensitively, to match how the tags are looked up
        # below — otherwise "abc" and "ABC" would both survive and resolve to
        # the same device, inflating the reported count.
        norm = tag.upper()
        if norm in seen:
            duplicates.append(tag)
            continue
        seen.add(norm)
        tags.append(tag)

    if not tags:
        return JSONResponse({"found": [], "not_found": [], "errors": errors,
                             "rows_read": rows_read, "duplicates": duplicates})

    # Matched case-insensitively: tag numbers get typed into spreadsheets by
    # hand and come back lowercase, while the stored barcode is upper. An exact
    # IN() reported every one of those as "not found" against a tag that plainly
    # exists.
    rows = (await db.execute(
        select(Device.barcode).where(
            func.upper(Device.barcode).in_([t.upper() for t in tags]),
            Device.is_trashed == False,
        )
    )).scalars().all()
    # Return the barcode as STORED, not as typed — the page ticks rows by exact
    # barcode string, so echoing the user's casing back would select nothing.
    by_upper = {}
    for stored in rows:
        by_upper.setdefault((stored or "").upper(), []).append(stored)

    found, not_found = [], []
    for t in tags:
        matches = by_upper.get(t.upper())
        if matches:
            found.extend(matches)
        else:
            not_found.append(t)

    # rows_read / duplicates are reported so the count on screen can be
    # reconciled against the file. Silently collapsing duplicates and then
    # showing only a matched count is what made a 390-row file look like a
    # different number entirely.
    return JSONResponse({"found": found, "not_found": not_found, "errors": errors,
                         "rows_read": rows_read, "duplicates": duplicates})


# ── 1. INVENTORY SEARCH ──────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def device_search(
    request: Request,
    q: str = "",
    stage: str = "",
    lot: str = "",
    grade: str = "",
    category: str = "",
    device_type: str = "",
    date_from: str = "",
    date_to: str = "",
    employee: str = "",
    entity: str = "",
    exclude_sold: str = "",
    fs: str = "",
    stock_type: str = "",
    pna: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Global inventory browser — search across all stages.

    Rows for the main table come from /devices/data (DataTables server-side).
    This handler used to fetch every matching device — up to ~19,000 with no
    filter applied — just to render them and to build location_map /
    stock_price_map / sale_price_map, which existed solely to feed cells in
    that table. Those maps are now built per-page inside /devices/data instead,
    so fetching the full result set here would cost real query time and memory
    for output nothing on the page uses.
    """
    filters = _device_search_filters(q, stage, lot, grade, category, device_type, date_from, date_to,
                                     entity=entity, exclude_sold=_exclude_sold(exclude_sold, fs),
                                     stock_type=stock_type, pna=pna)
    # The Employee filter was removed from this page (2026-10-13); an old
    # link's ?employee= is ignored here and in the table feed's JS params.

    count_query = (
        select(func.count())
        .select_from(Device)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.is_trashed == False)
    )
    for f in filters:
        count_query = count_query.where(f)
    total = (await db.execute(count_query)).scalar() or 0

    # Lot list for filter dropdown
    lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
    lots = lots_result.scalars().all()

    lot_summary = await _build_lot_summary(db, filters)
    # Model Summary rows load by AJAX (/devices/model-summary/data); only its
    # Make / Device Type filter options are needed up front.
    _ms_opts = (await db.execute(
        select(Device.brand, Device.device_type).select_from(Device)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.is_trashed == False, *filters).distinct()  # noqa: E712
    )).all()
    ms_make_options = sorted({b or MS_UNKNOWN_MAKE for b, _ in _ms_opts}, key=str.lower)
    ms_dtype_options = sorted({d or MS_NONE for _, d in _ms_opts}, key=str.lower)

    # Per-entity counts over the currently filtered set (not a separate
    # unfiltered query) — the summary strip above the Tag Number table.
    entity_counts_rows = (await db.execute(
        select(Device.entity, func.count())
        .select_from(Device).join(Lot, Device.lot_id == Lot.id)
        .where(Device.is_trashed == False, *filters)
        .group_by(Device.entity)
    )).all()
    entity_counts = {(e or "Unassigned"): c for e, c in entity_counts_rows}

    entity_options = await entity_values(db)
    storage_locations = (await db.execute(
        select(StorageLocation).where(StorageLocation.is_active == True)  # noqa: E712
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )).scalars().all()

    return templates.TemplateResponse("devices/list.html", {
        "request": request, "current_user": current_user,
        "lots": lots,
        "stages": DeviceStage, "stage_labels": STAGE_LABELS,
        # (value, label) pairs for the multi-select filter dropdowns. All
        # Inventory includes Sold here (unlike DROPDOWN_STAGES' shared
        # default, used elsewhere) so "Active Stock Only" can be overridden
        # per-stage — picking Stage=Sold while the checkbox stays checked
        # needs Sold to actually be a selectable option.
        "stage_choices": [(s.value, STAGE_LABELS.get(s, s.value))
                           for s in DeviceStage if s != DeviceStage.l2],
        "lot_choices": [l.lot_number for l in lots if l.lot_number],
        "q": q, "stage": stage, "lot": lot, "grade": grade, "category": category,
        "device_type": device_type, "entity": entity, "stock_type": stock_type, "pna": pna,
        "stock_type_options": [("finish_good", "Finish Good"), ("as_is", "As-Is"),
                               ("in_progress", "In Progress")],
        "exclude_sold": _exclude_sold(exclude_sold, fs),
        "device_type_options": await master_values(db, "device_type"),
        "entity_options": entity_options,
        "stage_options": [(s.value, STAGE_LABELS.get(s, s.value)) for s in DROPDOWN_STAGES],
        "storage_locations": storage_locations,
        "total": total,
        "lot_summary": lot_summary,
        "ms_make_options": ms_make_options,
        "ms_dtype_options": ms_dtype_options,
        "entity_counts": entity_counts,
    })


async def _build_lot_summary(db: AsyncSession, filters: list | None = None) -> list:
    """Lot Based table (below the Model Based table on Inventory Search):
    one row per Lot with GRN/supplier/vendor/financial details plus
    'Actual Selling' — the sum of Sale.sale_price for devices in that lot.

    Honours the page filters, which it previously ignored entirely: the Model
    Based table narrowed as you filtered while this one kept listing every lot
    in the system, so the two tables on the same screen answered different
    questions. Only lots holding at least one matching device are listed, and
    every per-lot figure below is computed over the matching devices alone.
    """
    lot_q = select(Lot).where(Lot.is_trashed == False)
    if filters:
        # Narrow to lots that still have a device matching the filters. Same
        # Device->Lot join the count query uses, so the two cannot disagree.
        matching = (
            select(Device.lot_id)
            .select_from(Device).join(Lot, Device.lot_id == Lot.id)
            .where(Device.is_trashed == False, *filters)
        )
        lot_q = lot_q.where(Lot.id.in_(matching))
    lots = (await db.execute(lot_q.order_by(Lot.lot_number))).scalars().all()
    if not lots:
        return []

    actual_selling_rows = (await db.execute(
        select(Device.lot_id, func.sum(Sale.sale_price))
        .join(Sale, Sale.device_id == Device.id)
        .where(Device.lot_id.in_([l.id for l in lots]))
        .group_by(Device.lot_id)
    )).all()
    actual_selling_map = {str(lot_id): float(total or 0) for lot_id, total in actual_selling_rows}

    # Per-lot tag numbers (Device -> barcode/qty) for the View modal, plus
    # the distinct Device Type(s) present in each lot for the summary column.
    device_q = (
        select(Device.lot_id, Device.barcode, Device.qty, Device.device_type)
        .select_from(Device).join(Lot, Device.lot_id == Lot.id)
        .where(Device.lot_id.in_([l.id for l in lots]), Device.is_trashed == False)
    )
    if filters:
        device_q = device_q.where(*filters)
    device_rows = (await db.execute(device_q)).all()
    tags_by_lot: dict = {}
    device_types_by_lot: dict = {}
    matched_qty: dict = {}
    for lot_id, barcode, qty, device_type in device_rows:
        tags_by_lot.setdefault(str(lot_id), []).append({"barcode": barcode, "qty": qty or 1})
        matched_qty[str(lot_id)] = matched_qty.get(str(lot_id), 0) + (qty or 1)
        if device_type:
            device_types_by_lot.setdefault(str(lot_id), set()).add(device_type)

    summary = []
    for l in lots:
        summary.append({
            "lot_number": l.lot_number,
            "purchase_date": l.purchase_date,
            "supplier_name": l.supplier_name,
            "grn_date": l.grn_date,
            "vendor_name": l.vendor_name or "—",
            # Filtered: the units actually matching. Unfiltered: the lot's own
            # recorded quantity, which can differ from the device count.
            "qty": matched_qty.get(str(l.id), 0) if filters else l.qty,
            "condition": l.condition or "—",
            "device_type": ", ".join(sorted(device_types_by_lot.get(str(l.id), []))) or "—",
            "buying_price": float(l.buying_price or 0),
            "selling_price": float(l.selling_price) if l.selling_price is not None else None,
            "actual_selling": actual_selling_map.get(str(l.id), 0.0),
            "notes": l.notes or "—",
            "tags": tags_by_lot.get(str(l.id), []),
        })
    return summary


def _device_price_expr(costing):
    """Stock Price per device, matching the main table's rule exactly.

    A device_costing row wins even when its total_cost is NULL (that reads as
    zero, not "fall back to the purchase price"); only a device with no costing
    row at all uses device_price x qty. `qty or 1` treats 0 as 1, so a plain
    COALESCE would be wrong for zero-qty rows.
    """
    units = case((func.coalesce(Device.qty, 0) > 0, Device.qty), else_=1)
    return case(
        (costing.c.device_id.isnot(None), func.coalesce(costing.c.total_cost, 0.0)),
        else_=func.coalesce(Device.device_price * units, 0.0),
    )


async def _build_model_summary(db: AsyncSession, filters: list) -> list:
    """Model Based table (below the main Devices table on Inventory Search):
    one row per distinct (model, brand), aggregated across ALL matching devices.

    Aggregated in SQL. This used to select every matching device — 22,395 rows
    in production — and group them in Python, then embed each group's full tag
    list in the page HTML. That was ~5s of query time plus a very large page.
    Postgres now returns one row per group (619 of them) and the per-tag detail
    the view modal needs is fetched on demand from /devices/model-summary/tags.

    Grouping is on the raw columns rather than COALESCE(...) because Postgres
    will not match a COALESCE in GROUP BY to the same expression in the SELECT
    list when each carries its own bind parameter. NULL and empty-string models
    are folded together afterwards, which is what `model or "Unknown Model"` did.
    """
    scope = [Device.is_trashed == False, *filters]

    # Both joins must contribute at most one row per device, or the counts and
    # the price sum inflate. No device has two device_costing rows today, but
    # collapsing here means a future duplicate cannot silently double a total.
    costing = (
        select(DeviceCosting.device_id.label("device_id"),
               func.max(DeviceCosting.total_cost).label("total_cost"))
        .group_by(DeviceCosting.device_id).subquery()
    )
    consumed = (
        select(SparePartConsumption.device_id.label("device_id"))
        .distinct().subquery()
    )

    rows = (await db.execute(
        select(
            Device.model,
            Device.brand,
            # The old Python version took whichever device happened to come back
            # first, so a group whose first row had no device_type displayed "—"
            # even when thousands of its devices said "Laptop" — and which row
            # came first was down to the planner. mode() is deterministic and
            # reports what the group actually mostly is.
            func.mode().within_group(Device.device_type).label("device_type"),
            func.count(Device.id).label("total_count"),
            func.sum(_device_price_expr(costing)).label("total_price"),
            func.count(consumed.c.device_id).label("repaired_count"),
            func.count(case((Device.grade == DeviceGrade.A, 1))).label("grade_a"),
            func.count(case((Device.grade == DeviceGrade.B, 1))).label("grade_b"),
            func.count(case((Device.grade == DeviceGrade.C, 1))).label("grade_c"),
        )
        .select_from(Device)
        .join(Lot, Device.lot_id == Lot.id)
        .outerjoin(costing, costing.c.device_id == Device.id)
        .outerjoin(consumed, consumed.c.device_id == Device.id)
        .where(*scope)
        .group_by(Device.model, Device.brand)
    )).all()

    # Fold NULL and "" into the Unknown buckets, merging any groups that collide.
    groups: dict = {}
    for r in rows:
        key = (r.model or "Unknown Model", r.brand or "Unknown Make")
        g = groups.setdefault(key, {
            "model": key[0], "make": key[1], "device_type": r.device_type or "—",
            "total_count": 0, "total_price": 0.0, "repaired_count": 0,
            "grade_counts": {"A": 0, "B": 0, "C": 0},
        })
        g["total_count"] += r.total_count or 0
        g["total_price"] += float(r.total_price or 0)
        g["repaired_count"] += r.repaired_count or 0
        g["grade_counts"]["A"] += r.grade_a or 0
        g["grade_counts"]["B"] += r.grade_b or 0
        g["grade_counts"]["C"] += r.grade_c or 0
        if g["device_type"] == "—" and r.device_type:
            g["device_type"] = r.device_type

    summary = []
    for g in groups.values():
        g["unit_price"] = round(g["total_price"] / g["total_count"], 2) if g["total_count"] else 0
        g["total_price"] = round(g["total_price"], 2)
        summary.append(g)
    summary.sort(key=lambda g: g["total_count"], reverse=True)
    return summary


# ── All Inventory: Model Summary (AJAX) ──────────────────────────────────────
# One row per distinct Model + Make + Device Type + CPU + RAM + Storage, built
# from the same devices the Tag table above shows: the page filters PLUS the Tag
# table's own search box (tsearch). Its own filters (s_*) narrow only this
# table. Per row: Touch / Grade / Stock Type / Entity breakdowns as counts.

MS_UNKNOWN_MODEL, MS_UNKNOWN_MAKE, MS_NONE = "Unknown Model", "Unknown Make", "—"
MS_GRADE_ORDER = ["A", "B", "C", "D", "E", "scrap", "NA"]
MS_STOCK_LABELS = {"finish_good": "Finish Good", "as_is": "As-Is", "in_progress": "In Progress"}
MS_TOUCH_LABELS = {"touch": "T", "non_touch": "NT"}


def entity_abbr(name: str | None) -> str:
    """'OxyPC Computers' -> 'OC', 'Renew Circuits' -> 'RC', 'Deshwal' -> 'DES'."""
    n = (name or "").strip()
    if not n or n == "Unassigned":
        return "NA"
    words = n.split()
    return "".join(w[0] for w in words).upper() if len(words) > 1 else n[:3].upper()


def _ms_ram_text(ram_summary, ram_gb) -> str:
    # Same rule as the Tag table's RAM cell.
    return ram_summary or (f"{ram_gb} GB" if ram_gb else MS_NONE)


def _ms_storage_text(hdd_summary, storage_gb, storage_type) -> str:
    # Same rule as the Tag table's Storage cell.
    return hdd_summary or (f"{storage_gb} GB {storage_type or ''}".strip() if storage_gb else MS_NONE)


def _ms_is_touch(touch_type) -> bool:
    return (touch_type or "").strip().lower() == "touch"


def _ms_null_or(col, values, null_label):
    """col IN values, where null_label stands for NULL / empty."""
    real = [v for v in values if v != null_label]
    clauses = [col.in_(real)] if real else []
    if len(real) != len(values):
        clauses.append(or_(col.is_(None), col == ""))
    return or_(*clauses)


# ── Stock Type (2026-10-15) — one rule for the table, its filter and sort,
# the Model Summary and the export:
#   As-Is        the tag has a Sub-Lot Number (any stage)
#   Finish Good  no Sub-Lot Number and the tag is Ready to Sale or Sold
#   In Progress  every other tag (still in the pipeline, returned, scrapped)
STOCK_TYPE_LABELS = {"as_is": "As-Is", "finish_good": "Finish Good", "in_progress": "In Progress"}
STOCK_TYPE_BADGE = {"as_is": "warning text-dark", "finish_good": "success", "in_progress": "secondary"}
_FINISH_GOOD_STAGES = (DeviceStage.ready_to_sale, DeviceStage.sold)


def stock_type_key(stage, sub_lot_number) -> str:
    if (sub_lot_number or "").strip():
        return "as_is"
    if stage in _FINISH_GOOD_STAGES or getattr(stage, "value", stage) in {s.value for s in _FINISH_GOOD_STAGES}:
        return "finish_good"
    return "in_progress"


def stock_type_sql():
    """SQL CASE giving the same key as stock_type_key()."""
    return case(
        (func.coalesce(func.trim(Device.sub_lot_number), "") != "", "as_is"),
        (Device.current_stage.in_(_FINISH_GOOD_STAGES), "finish_good"),
        else_="in_progress")


def _ms_own_filters(s_entity="", s_make="", s_device_type="", s_touch="", s_stock="", s_grade="") -> list:
    w = []
    if _multi(s_entity):
        w.append(_ms_null_or(Device.entity, _multi(s_entity), "Unassigned"))
    if _multi(s_make):
        w.append(_ms_null_or(Device.brand, _multi(s_make), MS_UNKNOWN_MAKE))
    if _multi(s_device_type):
        w.append(_ms_null_or(Device.device_type, _multi(s_device_type), MS_NONE))
    touch = set(_multi(s_touch))
    if touch and touch != {"touch", "non_touch"}:
        is_touch = func.lower(func.trim(func.coalesce(Device.touch_type, ""))) == "touch"
        w.append(is_touch if "touch" in touch else ~is_touch)
    stock = set(_multi(s_stock)) & set(STOCK_TYPE_LABELS)
    if stock and stock != set(STOCK_TYPE_LABELS):
        w.append(stock_type_sql().in_(sorted(stock)))
    grades = _multi(s_grade)
    if grades:
        real = []
        for g in grades:
            try:
                real.append(DeviceGrade(g))
            except ValueError:
                pass
        clauses = [Device.grade.in_(real)] if real else []
        if "NA" in grades:
            clauses.append(Device.grade.is_(None))
        if clauses:
            w.append(or_(*clauses))
    return w


async def _ms_page_scope(db, q, stage, lot, grade, category, device_type, date_from, date_to,
                         employee, entity, exclude_sold, fs, tsearch, stock_type="", pna="") -> list:
    """Exactly the devices the Tag table shows (its feed: /devices/data)."""
    f = _device_search_filters(q, stage, lot, grade, category, device_type, date_from, date_to,
                               entity=entity, exclude_sold=_exclude_sold(exclude_sold, fs),
                                     stock_type=stock_type, pna=pna)
    if employee:
        f.append(await _employee_device_id_filter(db, employee))
    t = _spec_search_clause(tsearch, extra_cols=(Lot.lot_number,))
    if t is not None:
        f.append(t)
    return f


def _ms_key(model, brand, device_type, cpu, ram_summary, ram_gb, hdd_summary, storage_gb, storage_type):
    return (model or MS_UNKNOWN_MODEL, brand or MS_UNKNOWN_MAKE, device_type or MS_NONE,
            (cpu or "").strip() or MS_NONE, _ms_ram_text(ram_summary, ram_gb),
            _ms_storage_text(hdd_summary, storage_gb, storage_type))


@router.get("/model-summary/data")
async def model_summary_data(
    q: str = "", stage: str = "", lot: str = "", grade: str = "", category: str = "",
    device_type: str = "", date_from: str = "", date_to: str = "", employee: str = "",
    entity: str = "", exclude_sold: str = "", fs: str = "", tsearch: str = "", stock_type: str = "", pna: str = "",
    s_entity: str = "", s_make: str = "", s_device_type: str = "",
    s_touch: str = "", s_stock: str = "", s_grade: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Rows for the All Inventory Model Summary table. Grouped in SQL on the raw
    columns (one row per distinct combination incl. entity / touch / stock /
    grade), then folded in Python into the displayed rows — the RAM / Storage
    text is derived from several columns, same as the Tag table."""
    from models.role_permissions import has_perm as _hp
    show_pricing = _hp(getattr(current_user.role, "value", current_user.role), "devices", "pricing")

    page_scope = await _ms_page_scope(db, q, stage, lot, grade, category, device_type, date_from,
                                      date_to, employee, entity, exclude_sold, fs, tsearch, stock_type, pna)
    scope = [Device.is_trashed == False, *page_scope]  # noqa: E712
    own = _ms_own_filters(s_entity, s_make, s_device_type, s_touch, s_stock, s_grade)

    costing = (
        select(DeviceCosting.device_id.label("device_id"),
               func.max(DeviceCosting.total_cost).label("total_cost"))
        .group_by(DeviceCosting.device_id).subquery()
    )
    # Raw columns only in GROUP BY — Postgres won't match an expression that
    # carries its own bind parameters (see _build_model_summary).
    cols = (Device.model, Device.brand, Device.device_type, Device.cpu, Device.ram_summary,
            Device.ram_gb, Device.hdd_summary, Device.storage_gb, Device.storage_type,
            Device.entity, Device.touch_type, Device.grade, Device.sub_lot_number,
            Device.current_stage)
    rows = (await db.execute(
        select(*cols, func.count(Device.id).label("n"),
               func.sum(_device_price_expr(costing)).label("price"))
        .select_from(Device).join(Lot, Device.lot_id == Lot.id)
        .outerjoin(costing, costing.c.device_id == Device.id)
        .where(*scope, *own)
        .group_by(*cols)
    )).all()

    groups: dict = {}
    for r in rows:
        key = _ms_key(r.model, r.brand, r.device_type, r.cpu, r.ram_summary, r.ram_gb,
                      r.hdd_summary, r.storage_gb, r.storage_type)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"model": key[0], "make": key[1], "device_type": key[2], "cpu": key[3],
                               "ram": key[4], "storage": key[5], "total": 0, "price": 0.0,
                               "entity": {}, "touch": {}, "stock": {}, "grade": {}}
        n = r.n or 0
        g["total"] += n
        g["price"] += float(r.price or 0)
        ent = (r.entity or "").strip() or "Unassigned"
        g["entity"][ent] = g["entity"].get(ent, 0) + n
        t = "T" if _ms_is_touch(r.touch_type) else "NT"
        g["touch"][t] = g["touch"].get(t, 0) + n
        st = STOCK_TYPE_LABELS[stock_type_key(r.current_stage, r.sub_lot_number)]
        g["stock"][st] = g["stock"].get(st, 0) + n
        gr = getattr(r.grade, "value", r.grade) or "NA"
        g["grade"][gr] = g["grade"].get(gr, 0) + n

    out = sorted(groups.values(), key=lambda g: g["total"], reverse=True)
    for g in out:
        g["price"] = round(g["price"], 2) if show_pricing else None

    # Make / Device Type options = what the Tag table's devices actually hold
    opt_rows = (await db.execute(
        select(Device.brand, Device.device_type).select_from(Device)
        .join(Lot, Device.lot_id == Lot.id).where(*scope).distinct()
    )).all()
    makes = sorted({b or MS_UNKNOWN_MAKE for b, _ in opt_rows}, key=str.lower)
    dtypes = sorted({d or MS_NONE for _, d in opt_rows}, key=str.lower)
    entities = list(await entity_values(db))
    for g in out:
        for e in g["entity"]:
            if e not in entities:
                entities.append(e)
    if "Unassigned" not in entities:
        entities.append("Unassigned")
    from utils.grades import grade_options as _grade_options
    return JSONResponse({
        "rows": out,
        "show_pricing": show_pricing,
        "entity_abbr": {e: entity_abbr(e) for e in entities},
        "grade_order": MS_GRADE_ORDER,
        "options": {
            "make": makes, "device_type": dtypes, "entity": entities,
            "grade": [[v, l] for v, l in _grade_options()] + [["NA", "No Grade"]],
        },
    })


@router.get("/model-summary/tags")
async def model_summary_tags(
    model: str = "", make: str = "", m_device_type: str = "", m_cpu: str = "",
    m_ram: str = "", m_storage: str = "",
    q: str = "", stage: str = "", lot: str = "", grade: str = "", category: str = "",
    device_type: str = "", date_from: str = "", date_to: str = "", employee: str = "",
    entity: str = "", exclude_sold: str = "", fs: str = "", tsearch: str = "", stock_type: str = "", pna: str = "",
    s_entity: str = "", s_make: str = "", s_device_type: str = "",
    s_touch: str = "", s_stock: str = "", s_grade: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Per-tag detail for one Model Summary row, loaded when the modal opens.
    Same scope as the row (page filters + Tag table search + summary filters),
    matched on all six grouping fields."""
    page_scope = await _ms_page_scope(db, q, stage, lot, grade, category, device_type, date_from,
                                      date_to, employee, entity, exclude_sold, fs, tsearch, stock_type, pna)
    own = _ms_own_filters(s_entity, s_make, s_device_type, s_touch, s_stock, s_grade)

    def _match(col, value, unknown_label):
        if value == unknown_label:
            return or_(col.is_(None), col == "")
        return col == value

    rows = (await db.execute(
        select(Device.barcode, Device.grade, Device.device_type, Device.cpu, Device.ram_summary,
               Device.ram_gb, Device.hdd_summary, Device.storage_gb, Device.storage_type,
               Device.model, Device.brand, StorageLocation.unit_id, StorageLocation.unit_type)
        .select_from(Device)
        .join(Lot, Device.lot_id == Lot.id)
        .outerjoin(StorageLocation, Device.location_id == StorageLocation.id)
        .where(Device.is_trashed == False, *page_scope, *own,  # noqa: E712
               _match(Device.model, model, MS_UNKNOWN_MODEL),
               _match(Device.brand, make, MS_UNKNOWN_MAKE))
        .order_by(Device.barcode)
    )).all()
    want = (model, make, m_device_type, m_cpu, m_ram, m_storage)
    tags = []
    for r in rows:
        key = _ms_key(r.model, r.brand, r.device_type, r.cpu, r.ram_summary, r.ram_gb,
                      r.hdd_summary, r.storage_gb, r.storage_type)
        # Fields not sent (older callers) are not compared
        if any(w and k != w for k, w in zip(key, want)):
            continue
        tags.append({
            "barcode": r.barcode,
            "location_id": r.unit_id or "—",
            "location_type": UNIT_TYPE_LABELS.get(r.unit_type, r.unit_type) if r.unit_type else "—",
            "grade": getattr(r.grade, "value", r.grade) if r.grade else "—",
        })
    return JSONResponse({"tags": tags})


_EXPORT_HEADER = [
    "Barcode", "Lot", "GRN", "Invoice No", "Entity", "Sub-Category", "Brand", "Model", "Device Type",
    # RAM / Storage only (2026-10-22) — replaced RAM GB, RAM, Total RAM
    # Count/Size, SSD GB, Storage Type, Hard Drive, HDD GB, Total HDD Count/Size.
    "Serial No", "CPU", "CPU Make", "Generation", "RAM", "Storage",
    "Screen Size", "Battery %", "BIOS Pwd", "Color",
    # ── Hardware / functional (IQC) — blank for a device with no IQC row yet ──
    "Power On", "Power Status", "HDD Connector", "HDD Casing", "Battery Present", "Battery Cable",
    "Charging Port", "DVD Drive", "Wi-Fi", "Web Cam", "Speaker", "Fan Working",
    "Keyboard Working", "Touchpad Working", "HDMI Port", "USB Port", "Audio Jack",
    "USB A Ports", "USB C Ports", "Ethernet Ports",
    # ── Cosmetics — one worst-severity column per Parts Consumption group ──
    "Display Panel Cosmetic", "Bezel Frame Cosmetic", "Screen Cosmetic", "Hinge Cosmetic",
    "Touchpad Cosmetic", "Bottom Base Cosmetic", "Palmrest Cosmetic",
    "Device Price", "Grade", "Stage", "Final QC Status", "Stage History",
    "Location ID", "Bin Location", "Zone", "Warehouse", "Notes", "Created", "Updated",
    "PNA", "Stock",
]


def _pna_label(device, pna_device_ids) -> str:
    """"Yes" iff the tag is in the Production Manager PNA summary: it has an
    active PNA part (id in pna_device_ids) AND sits in a PNA_SUMMARY_STAGE AND
    is active and not trashed — the same scope the summary page lists."""
    from services.pna_lookup import PNA_SUMMARY_STAGES
    in_scope = (
        device.id in pna_device_ids
        and device.current_stage in PNA_SUMMARY_STAGES
        and bool(device.is_active)
        and not device.is_trashed
    )
    return "Yes" if in_scope else "No"


def _stock_label(device) -> str:
    """Stock Type for the export — same rule as the table (stock_type_key)."""
    return STOCK_TYPE_LABELS[stock_type_key(device.current_stage, device.sub_lot_number)]


from utils.device_spec import ram_text, storage_text  # noqa: E402  (RAM / Storage export, 2026-10-22)


async def _export_rows(db: AsyncSession, rows) -> Response:
    """Shared CSV builder for both the filtered export and the selected-tags
    export — same header, same columns, same cosmetic/hardware lookups,
    so a selection export can never show fewer fields than a full one.

    Takes already-fetched (Device, lot_number) rows rather than a query, so a
    caller whose own WHERE...IN() list could exceed Postgres' 32767-bound-
    parameter limit (see CHUNK below) can fetch it in batches itself before
    handing the merged rows in here."""
    from services.part_estimate_matrix import PART_GROUPS

    import time as _time
    _t = [_time.perf_counter()]
    _phase = {}

    def _mark(name):
        now = _time.perf_counter()
        _phase[name] = _phase.get(name, 0) + (now - _t[0])
        _t[0] = now

    device_ids = [device.id for device, _ in rows]

    # Postgres' wire protocol caps bound parameters at 32767 (an Int16 count
    # field) — an unfiltered "All Inventory" export easily has more device_ids
    # than that (37,048 live 2026-09-07, asyncpg raised "the number of query
    # arguments cannot exceed 32767"), while any filtered/smaller export
    # stayed under it and looked fine. Chunking keeps this correct at any
    # inventory size instead of merely working today by coincidence.
    CHUNK = 20000
    id_chunks = [device_ids[i:i + CHUNK] for i in range(0, len(device_ids), CHUNK)] if device_ids else []

    movements_by_device, iqc_by_device, location_map = {}, {}, {}
    pna_device_ids = set()
    for chunk in id_chunks:
        # Chunked like the lookups below (active_pna_parts also chunks
        # internally, but this keeps each call well under the param cap).
        # Speed (2026-10-19): only the columns each lookup needs, as plain
        # rows — loading every StageMovement / PNA part as a full ORM object
        # made an ~11,700-tag export take 20+ seconds.
        pna_device_ids.update((await db.execute(
            select(DevicePNAPart.device_id).where(
                DevicePNAPart.device_id.in_(chunk), DevicePNAPart.is_active == True)  # noqa: E712
            .distinct()
        )).scalars().all())
        _mark("pna")

        mv_result = await db.execute(
            select(StageMovement.device_id, StageMovement.to_stage, StageMovement.moved_at)
            .where(StageMovement.device_id.in_(chunk))
            .order_by(StageMovement.moved_at.asc())
        )
        for did, to_stage, moved_at in mv_result.all():
            movements_by_device.setdefault(did, []).append((to_stage, moved_at))
        _mark("moves")

        iqc_result = await db.execute(
            select(IQCInspection).where(IQCInspection.device_id.in_(chunk))
        )
        for iqc in iqc_result.scalars().all():
            iqc_by_device[iqc.device_id] = iqc
        _mark("iqc")

        # _build_location_map issues its own un-chunked .in_() query, so it
        # must be called per-chunk here too — otherwise a full "All
        # Inventory" export would reintroduce the exact 32767-bound-parameter
        # crash the movements/IQC chunking above already exists to avoid.
        location_map.update(await _build_location_map(db, [str(did) for did in chunk]))
        _mark("loc")

    cosmetic_parts = next(p for g, _h, _f, _s, p in PART_GROUPS if g == "cosmetic")

    def _stage_history(device):
        moves = movements_by_device.get(device.id)
        if not moves:
            return STAGE_LABELS.get(device.current_stage, device.current_stage)
        parts = []
        for to_stage, moved_at in moves:
            label = STAGE_LABELS.get(to_stage, to_stage)
            when = moved_at.strftime("%d-%b-%Y") if moved_at else ""
            parts.append(f"{label} ({when})" if when else label)
        return " -> ".join(parts)

    def _iqc_cols(iqc):
        if iqc is None:
            return [""] * 20  # must match the 20 values in the `else` branch below
        return [
            iqc.power_on, iqc.status, iqc.hdd_connector, iqc.hdd_casing,
            iqc.battery_present, iqc.battery_cable, iqc.charging_port, iqc.dvd_drive,
            iqc.wifi_status, iqc.webcam_status, iqc.speaker_status, iqc.fan_working,
            iqc.keyboard_working, iqc.touchpad_working, iqc.port_hdmi,
            iqc.port_usb_working, iqc.port_audio_jack,
            iqc.usb_a_ports, iqc.usb_c_ports, iqc.ethernet_ports,
        ]

    def _cosmetic_cols(iqc):
        # Reuses the same worst-severity classifiers Part Estimate prices
        # against, so this column can never disagree with what a "Major"
        # filter there would have counted.
        if iqc is None:
            return [""] * len(cosmetic_parts)
        return [fn(iqc, None) for _name, fn in cosmetic_parts]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_EXPORT_HEADER)
    for device, lot_number in rows:
        iqc = iqc_by_device.get(device.id)
        loc_info = location_map.get(str(device.id)) or {}
        # Same resolution order as /api/brief's own location display —
        # most-recent DeviceLocationLog entry, falling back to the legacy
        # free-text fields when a device has no logged location yet.
        location_display = loc_info.get("unit_id") or device.warehouse or device.floor or ""
        zone_display = ZONE_LABELS.get(loc_info.get("zone"), "") if loc_info.get("zone") else ""
        row = [
            device.barcode, lot_number, device.grn_number, device.invoice_number, device.entity,
            device.sub_category, device.brand, device.model, device.device_type, device.serial_no,
            device.cpu, device.cpu_make, device.generation,
            ram_text(device), storage_text(device), device.screen_size,
            device.battery_health_pct, "Yes" if device.bios_password else "No",
            device.color,
            *_iqc_cols(iqc),
            *_cosmetic_cols(iqc),
            device.device_price if device.device_price is not None else "",
            device.grade,
            STAGE_LABELS.get(device.current_stage, device.current_stage),
            device.final_qc_status or "",
            _stage_history(device),
            location_display,
            device.bin_location or loc_info.get("slot") or "",
            zone_display, device.warehouse, device.notes,
            # Year-first so Excel reads real dates on any PC locale (2026-10-21).
            device.created_at.strftime("%Y-%m-%d %H:%M") if device.created_at else "",
            device.updated_at.strftime("%Y-%m-%d %H:%M") if device.updated_at else "",
            _pna_label(device, pna_device_ids),
            _stock_label(device),
        ]
        # A silent column-count mismatch (e.g. an IQC/cosmetic helper
        # returning the wrong placeholder count for a device missing that
        # data) shifts every later column right for just THAT row — hard to
        # spot in the file itself, since CSV columns are positional, not
        # name-matched. Fail loudly instead of exporting misaligned data.
        assert len(row) == len(_EXPORT_HEADER), (
            f"Export row for {device.barcode} has {len(row)} columns, "
            f"expected {len(_EXPORT_HEADER)} — column mapping is out of sync with _EXPORT_HEADER."
        )
        writer.writerow(row)
    _mark("csv")
    body = output.getvalue().encode("utf-8-sig")
    _mark("encode")
    # Server-Timing (2026-10-19): per-step milliseconds, visible in the
    # browser's network panel — how slow exports were tracked down.
    timing = ", ".join(f"{k};dur={v * 1000:.0f}" for k, v in _phase.items())
    # A plain Response, not StreamingResponse(BytesIO): iterating a BytesIO
    # yields one CSV line per chunk, so an 11,700-row file went out as
    # ~11,700 separate writes — about 10 of the 17 seconds the export took.
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=devices_export.csv",
                 "Server-Timing": timing},
    )


@router.get("/export")
async def export_devices(
    q: str = "",
    stage: str = "",
    lot: str = "",
    grade: str = "",
    category: str = "",
    device_type: str = "",
    date_from: str = "",
    date_to: str = "",
    entity: str = "",
    stock_type: str = "",
    pna: str = "",
    exclude_sold: str = "",
    fs: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Export the current filtered search as CSV — every field the page shows
    plus Entity, hardware condition, cosmetic condition and device price.

    Filters go through the same _device_search_filters the page's own data
    endpoint uses, so what's on screen is always what gets exported — a
    hand-duplicated filter list here previously fell out of step with the
    page (missing category, missing is_trashed, an inner join that dropped
    lot-less devices), which made the export look "empty" relative to the list.
    """
    # A single query, not chunked: _device_search_filters' own IN()s are
    # small enumerated checkbox lists (stage/grade/category/...), never
    # anywhere near Postgres' 32767-bound-parameter limit regardless of how
    # many devices actually match — that risk is in the *result* row count,
    # which _export_rows' own device_ids lookups chunk internally.
    query = (
        select(Device, Lot.lot_number)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(Device.is_trashed == False,
              *_device_search_filters(q, stage, lot, grade, category, device_type,
                                      date_from, date_to, entity=entity, stock_type=stock_type, pna=pna,
                                      exclude_sold=_exclude_sold(exclude_sold, fs)))
        .order_by(Device.updated_at.desc())
    )
    import time as _time
    t0 = _time.perf_counter()
    rows = (await db.execute(query)).all()
    resp = await _export_rows(db, rows)
    resp.headers["Server-Timing"] = (f"total;dur={(_time.perf_counter() - t0) * 1000:.0f}, "
                                     + resp.headers.get("Server-Timing", ""))
    return resp


@router.post("/export")
async def export_devices_selected(
    barcodes: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Export only the Tag Numbers ticked on the page, same columns as the
    filtered export. POST (not a long querystring) because a full-page
    selection can run into the hundreds of tags — or, via "select all"
    against an unfiltered/lightly-filtered search, tens of thousands; chunked
    for the same 32767-bound-parameter reason _export_rows chunks device_ids."""
    barcodes = [b for b in (barcodes or []) if b]
    if not barcodes:
        raise HTTPException(400, "No Tag Numbers selected")
    CHUNK = 20000
    rows = []
    for i in range(0, len(barcodes), CHUNK):
        chunk_query = (
            select(Device, Lot.lot_number)
            .outerjoin(Lot, Device.lot_id == Lot.id)
            .where(Device.is_trashed == False, Device.barcode.in_(barcodes[i:i + CHUNK]))
            .order_by(Device.updated_at.desc())
        )
        rows.extend((await db.execute(chunk_query)).all())
    return await _export_rows(db, rows)


# ── 2. DEVICE DETAIL ─────────────────────────────────────────────────────────

@router.get("/{barcode}", response_class=HTMLResponse)
async def device_detail(
    barcode: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(view_allowed),
):
    """Full device profile — specs, stage timeline, repairs, QC, parts consumed."""
    device = await _get_device_or_404(barcode, db)

    # Lot
    lot_result = await db.execute(select(Lot).where(Lot.id == device.lot_id))
    lot = lot_result.scalar_one_or_none()

    # Stage movements — newest first (2026-10-17), same order as the Search
    # to View Asset History modal (/devices/api/asset-history above).
    movements_result = await db.execute(
        select(StageMovement)
        .where(StageMovement.device_id == device.id)
        .order_by(StageMovement.moved_at.desc().nullslast())
    )
    movements = movements_result.scalars().all()

    # Repair jobs
    repairs_result = await db.execute(
        select(RepairJob)
        .where(RepairJob.device_id == device.id)
        .order_by(RepairJob.started_at.desc())
    )
    repairs = repairs_result.scalars().all()

    # QC checks
    qc_result = await db.execute(
        select(QCCheck)
        .where(QCCheck.device_id == device.id)
        .order_by(QCCheck.checked_at.desc())
    )
    qc_checks = qc_result.scalars().all()

    # Spare parts consumed
    parts_result = await db.execute(
        select(SparePartConsumption, SparePart.name, SparePart.category)
        .join(SparePart, SparePartConsumption.part_id == SparePart.id)
        .where(SparePartConsumption.device_id == device.id)
        .order_by(SparePartConsumption.used_at.desc())
    )
    parts_consumed = parts_result.all()
    total_parts_cost = sum(float(p.SparePartConsumption.total_cost or 0) for p in parts_consumed)

    # Avg lot cost per device (for device P&L)
    lot_cost_per_device = (
        float(lot.buying_price or 0) / max(lot.qty, 1) if lot else 0
    )

    # Current location for this device
    loc_map = await _build_location_map(db, [device.id])
    current_location = loc_map.get(str(device.id))

    # Assigned Storage Location (Location ID / Location Type / Zone). Reads
    # off the same DeviceLocationLog entry as current_location above, NOT
    # device.location_id — the "assign one" / pickup-placeback flow at
    # /locations/device/{id} only ever writes to the log, never to
    # device.location_id, so a device assigned that way (the normal path)
    # showed no Location ID anywhere on this page, All Inventory, or
    # Inventory Manager despite genuinely having one. device.location_id is
    # only ever set from Edit Device's own Assign Location dropdown or a
    # warehouse transfer, so it's kept as a fallback for a device that was
    # assigned that way and has no log entry at all.
    assigned_location = None
    log_location_id = current_location.get("location_id") if current_location else None
    resolved_location_id = log_location_id or device.location_id
    if resolved_location_id:
        assigned_location = (await db.execute(
            select(StorageLocation).where(StorageLocation.id == resolved_location_id)
        )).scalar_one_or_none()

    # IQC inspection (for stress report display)
    iqc_result = await db.execute(
        select(IQCInspection).where(IQCInspection.device_id == device.id)
    )
    iqc_inspection = iqc_result.scalar_one_or_none()

    # Parse stress report JSON for template rendering
    stress_data = None
    if iqc_inspection and iqc_inspection.stress_report:
        try:
            import json as _json
            stress_data = _json.loads(iqc_inspection.stress_report)
        except Exception:
            stress_data = None

    # ── Parts Consumption (#10): fixed parts list, IQC-driven Required flag,
    #    live stock status, and any existing engineer part-request state. ───────
    # include_master_extras: anything the admin adds to the Spare Part Names
    # dropdown shows up under ADDITIONAL PARTS without a code change.
    required_rows = compute_required(iqc_inspection, device, include_master_extras=True)
    pna_parts = set((await db.execute(
        select(DevicePNAPart.part_name).where(
            DevicePNAPart.device_id == device.id,
            DevicePNAPart.is_active.is_(True),
        )
    )).scalars().all())
    pr_rows = (await db.execute(
        select(PartRequest).where(PartRequest.device_id == device.id)
        .order_by(PartRequest.created_at.desc())
    )).scalars().all()
    req_by_part = {}
    req_by_partid = {}
    # Parts Consumption statuses are per VISIT: only requests raised since the
    # tag's latest Final QC pass drive the action column. A tag that passed
    # (or was sold) and came back starts every part clean — New / Replace —
    # instead of being stuck on an old "Part Changed" with no buttons. Earlier
    # changes stay in the spec card's Updated Parts list, and in
    # prev_changed_by_part for the small "changed in an earlier visit" note.
    # Still-open requests (requested / handed over) always count, whenever
    # they were raised, so nothing in flight can disappear.
    last_fqc_pass = (await db.execute(
        select(func.max(StageMovement.moved_at)).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == DeviceStage.final_qc_pass_hold)
    )).scalar()
    prev_changed_by_part = {}
    for r in pr_rows:
        # A cancelled request is not a request. Leaving it here pinned the row
        # to a "cancelled" badge with no buttons, so cancelling a request left
        # the engineer permanently unable to raise another for that part. Skip
        # it and the latest surviving request wins; if there is none, the New /
        # Replace buttons come back.
        if r.status == "cancelled":
            continue
        if (last_fqc_pass is not None and r.created_at is not None
                and r.created_at <= last_fqc_pass
                and r.status not in ("requested", "handed_over")):
            if r.status == "received":
                prev_changed_by_part.setdefault(r.part_name, r)
            continue
        req_by_part.setdefault(r.part_name, r)  # latest per part (rows ordered desc)
        if r.part_id:
            req_by_partid.setdefault(str(r.part_id), r)  # latest per resolved SparePart

    # ── Parts Consumed table (#33): every Part Request row whose Action
    #    flipped to "Part Changed" (status == "received"), priced from the
    #    live Part Master unit price × the qty actually handed over/verified.
    changed_part_ids = {r.part_id for r in pr_rows if r.status == "received" and r.part_id}
    changed_spare_parts = {}
    if changed_part_ids:
        sp_rows = (await db.execute(select(SparePart).where(SparePart.id.in_(changed_part_ids)))).scalars().all()
        changed_spare_parts = {sp.id: sp for sp in sp_rows}
    changed_parts_consumed = []
    for r in pr_rows:
        if r.status != "received":
            continue
        sp = changed_spare_parts.get(r.part_id)
        unit_price = float(sp.unit_price) if sp else 0.0
        qty = r.qty_handed_over or 0
        changed_parts_consumed.append({
            "part_name": r.part_name, "unit_price": unit_price,
            "qty": qty, "total": unit_price * qty,
        })
    total_changed_parts_cost = sum(row["total"] for row in changed_parts_consumed)

    # Net "In Stock" mirrors the Parts Dashboard: qty_in_stock minus the qty
    # already handed over (status="handed_over") but not yet deducted from raw
    # stock, so Stock Status here equals the Part Master In Stock column.
    consumed_rows = (await db.execute(
        select(PartRequest.part_id, func.sum(PartRequest.qty_handed_over))
        .where(PartRequest.status == "handed_over", PartRequest.part_id.isnot(None))
        .group_by(PartRequest.part_id)
    )).all()
    consumed_by_part = {str(pid): int(total or 0) for pid, total in consumed_rows}

    # Resolve every Parts Consumption row to its Part Master row by NAME in a
    # single query. Two reasons this is one query and not one-per-row: the DB
    # lives in a different region from the app, so a per-row loop pays the
    # round-trip latency N times; and matching by name (not category/keyword)
    # is what makes this table agree with the Parts Dashboard, which keys its
    # In Stock column off the Part Master row itself.
    # is_trashed==False mirrors the dashboard's own filter — a trashed part is
    # not "stock you can use", and including it here would over-report.
    wanted_names = {row["label"].strip().lower() for row in required_rows}
    sp_by_name = {}
    if wanted_names:
        name_rows = (await db.execute(
            select(SparePart).where(
                SparePart.is_trashed == False,
                func.lower(func.trim(SparePart.name)).in_(wanted_names),
            ).order_by(SparePart.qty_in_stock.desc())
        )).scalars().all()
        for sp_row in name_rows:
            # Ordered by stock desc, so setdefault keeps the best-stocked row
            # when a name is duplicated in Part Master.
            sp_by_name.setdefault((sp_row.name or "").strip().lower(), sp_row)

    # Category roll-up for the labels with no exact name match.
    #
    # PARTS_MATRIX labels are generic component types ("Keyboard", "Display");
    # Part Master rows are specific SKUs ("HP UK Keyboard", "15.6-inch HD Panel").
    # That is one-to-many, so an exact name match resolves for almost none of them
    # and there is no single "correct" SKU to show. Summing the category answers
    # the question the engineer is actually asking — do we have a keyboard on the
    # shelf — and every SKU in the sum is netted the same way the Parts Dashboard
    # nets it, so the two pages still reconcile.
    wanted_cats = {row["category"] for row in required_rows if row.get("category")}
    cat_totals: dict[str, int] = {}
    if wanted_cats:
        cat_rows = (await db.execute(
            select(SparePart).where(
                SparePart.is_trashed == False,
                SparePart.category.in_(wanted_cats),
            )
        )).scalars().all()
        for sp_row in cat_rows:
            net = max(0, int(sp_row.qty_in_stock or 0)
                         - consumed_by_part.get(str(sp_row.id), 0))
            cat_totals[sp_row.category] = cat_totals.get(sp_row.category, 0) + net

    parts_consumption = []
    for row in required_rows:
        sp = sp_by_name.get(row["label"].strip().lower())
        rolled_up = False
        if sp is not None:
            raw_stock = int(sp.qty_in_stock or 0)
            stock = max(0, raw_stock - consumed_by_part.get(str(sp.id), 0))
        elif row.get("category") in cat_totals:
            # Flagged as a category total in the UI rather than shown bare — it
            # answers a broader question than "how many of this exact part", and
            # presenting it as the latter is how the old code went wrong.
            stock = cat_totals[row["category"]]
            rolled_up = True
        else:
            # Nothing in that category either. Showing 0 would read as "out of
            # stock" when the truth is "we have no record" — a purchasing
            # decision made on that is a wrong decision. Show "no match" instead.
            stock = None
        # Match an existing request either by the PARTS_MATRIX label (Faulty /
        # matched-part flow submits part_name == label) OR by the resolved
        # SparePart id (New/Replace via the category→name cascade submits the
        # SparePart's own name, which never equals the label). Without the id
        # fallback the "Requested" pill silently never appears for those.
        existing = req_by_part.get(row["label"])
        for _legacy in LEGACY_LABELS.get(row["label"], ()):
            # Requests raised before the "Screen / Display" -> "Display" and
            # "Adapter / Charger" -> "Adapter" renames stored the OLD label in
            # part_name. Without this the Requested/Verify pill would silently
            # vanish for every in-flight request on those two parts.
            if existing:
                break
            existing = req_by_part.get(_legacy)
        if not existing and sp:
            existing = req_by_partid.get(str(sp.id))
        parts_consumption.append({
            "label": row["label"],
            "section": row["section"],
            "category": sp.category if sp else row["category"],
            "required": row["required"],
            "matched": sp is not None,
            "rolled_up": rolled_up,
            "in_stock": stock is not None and stock > 0,
            "stock_qty": stock,
            "part_id": str(sp.id) if sp else "",
            "part_code": sp.part_code if sp else None,
            "request": existing,
            "prev_changed": prev_changed_by_part.get(row["label"]),
        })

    # ── All active spare parts, for the New Request/Replace modal's
    #    Part Category -> Part Name cascade (client-side filtered). ──────────
    all_parts_result = await db.execute(
        select(SparePart).where(SparePart.is_trashed == False)
        .order_by(SparePart.category, SparePart.name)
    )
    all_spare_parts = [
        {"id": str(sp.id), "name": sp.name, "category": sp.category,
         "make": sp.make, "model": sp.model}
        for sp in all_parts_result.scalars().all()
    ]

    # ── Work ID History — all WorkOrders assigned to this device ──────────────
    work_orders = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device.id)
        .order_by(WorkOrder.assigned_at.desc())
    )).scalars().all()

    # ── Warranty Status for device detail ────────────────────────────────────
    _sale_for_warranty = (await db.execute(
        select(Sale).where(Sale.device_id == device.id).order_by(Sale.sold_at.desc()).limit(1)
    )).scalars().first()
    _w = effective_warranty_for_sale(_sale_for_warranty)
    if device.current_stage != DeviceStage.sold:
        warranty_label = "Not Sold Yet"
    elif _w and _w["status"] == "active":
        warranty_label = "Within Warranty"
    else:
        warranty_label = "Out of Warranty"

    # ── Repair Status (item 9): derived from current stage + repair jobs ──────
    repair_status = None
    _lvl = {DeviceStage.l1: 1, DeviceStage.l2: 2, DeviceStage.l3: 3}.get(device.current_stage)
    if _lvl:
        _REQ = ("Request to L2", "Request to L3", "Escalate to L2", "Escalate to L3")
        in_prog = any(r.stage == f"L{_lvl}" and r.status == RepairStatus.in_progress for r in repairs)
        latest = repairs[0] if repairs else None
        if latest and (latest.final_status or "") in _REQ and not in_prog:
            repair_status = f"Request to L{_lvl}"
        elif in_prog:
            repair_status = f"In Progress at L{_lvl}"
        else:
            repair_status = f"Pending at L{_lvl}"

    return templates.TemplateResponse("devices/detail.html", {
        "request": request, "current_user": current_user,
        "device": device, "lot": lot,
        "movements": movements, "repairs": repairs,
        "qc_checks": qc_checks, "parts_consumed": parts_consumed,
        "total_parts_cost": total_parts_cost,
        "lot_cost_per_device": lot_cost_per_device,
        "warranty_label": warranty_label,
        "stage_labels": STAGE_LABELS,
        "current_location": current_location,
        "assigned_location": assigned_location,
        "iqc_inspection": iqc_inspection,
        "stress_data": stress_data,
        "parts_consumption": parts_consumption,
        # Part names currently marked PNA for this tag — drives the
        # "Mark As" checkboxes and the counts on the L1/L2 + L3/L4 queues.
        "pna_parts": pna_parts,
        "changed_parts_consumed": changed_parts_consumed,
        "total_changed_parts_cost": total_changed_parts_cost,
        # Hardware Specification card's "Changed" badges + Updated Parts list
        "changed_parts": (await changed_parts_by_device(db, [device.id])).get(str(device.id)),
        "parts_badges": await parts_badge_map(db, [device.id]),
        "all_spare_parts": all_spare_parts,
        "work_orders": work_orders,
        "repair_status": repair_status,
    })


# ── 3. DEVICE EDIT ───────────────────────────────────────────────────────────

def _device_edit_boundary(fn):
    """Never let Device Edit fail with a bare 500.

    Mirrors routers/iqc.py's _iqc_form_boundary for the exact same reason:
    an unhandled exception here used to reach the user as an opaque 500 with
    their edits lost and nothing to report. Now it rolls back, expunges
    current_user so it stays readable if anything downstream (e.g. the
    request-scoped audit/logging middleware) touches it after rollback, logs
    a full traceback + the submitted form data server-side, and redirects
    back to the edit form with a readable error — reusing the ?error=...
    query param this same route's barcode-clash branch already redirects
    with, and templates/devices/edit.html already renders.
    """
    import functools
    import traceback as _tb

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            db = kwargs.get("db")
            barcode = kwargs.get("barcode", "?")
            current_user = kwargs.get("current_user")
            form_snapshot = {k: v for k, v in kwargs.items() if k not in ("db", "request", "current_user", "_perm")}
            # A failure mid-flush (e.g. autoflush hitting an FK violation
            # before this except block even runs) leaves the session in
            # SQLAlchemy's "pending rollback" state — where reading ANY
            # not-yet-loaded attribute on current_user (getattr's default
            # only covers a missing attribute, not one that raises while
            # being read) tries to lazy-refresh against the poisoned
            # session and raises PendingRollbackError right here, before
            # rollback() below ever runs. Found 2026-09-02 chasing exactly
            # this: the log line meant to help diagnose the failure was
            # itself throwing a second, masking exception.
            try:
                username_for_log = current_user.username if current_user is not None else "?"
            except Exception:
                username_for_log = "?"
            print(f"\n{'='*60}\nDEVICE EDIT FAILED for "
                  f"{username_for_log} barcode={barcode!r}\n"
                  f"form={form_snapshot!r}\n"
                  f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}{'='*60}",
                  flush=True)
            if db is not None:
                # Expunge BEFORE rollback (see routers/iqc.py's
                # _keep_after_rollback docstring for the full story — briefly
                # "fixed" the other way round and made it worse): rollback()
                # expires every attribute the session is tracking, so
                # detaching current_user first preserves whatever's already
                # loaded as plain in-memory values. If the session is already
                # in a "pending rollback" state (a failure mid-flush) expunge()
                # can itself raise here — harmlessly: it's caught, and
                # rollback() still runs next regardless, clearing that state.
                if current_user is not None:
                    try:
                        db.expunge(current_user)
                    except Exception:
                        pass
                try:
                    await db.rollback()
                except Exception:
                    pass
            import urllib.parse
            msg = f"Could not save changes — {type(exc).__name__}: {str(exc)[:200]}. Please screenshot this message."
            return RedirectResponse(
                url=f"/devices/{barcode}/edit?error={urllib.parse.quote(msg)}",
                status_code=302)

    return wrapper


@router.get("/{barcode}/edit", response_class=HTMLResponse)
async def device_edit_form(
    barcode: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(edit_allowed),
):
    device = await _get_device_or_404(barcode, db)
    lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
    lots = lots_result.scalars().all()
    storage_locs_result = await db.execute(
        select(StorageLocation)
        .where(StorageLocation.is_active == True)
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )
    storage_locations = storage_locs_result.scalars().all()
    movements_result = await db.execute(
        select(StageMovement)
        .where(StageMovement.device_id == device.id)
        .order_by(StageMovement.moved_at.desc())
    )
    movements = movements_result.scalars().all()
    iqc_inspection = (await db.execute(
        select(IQCInspection).where(IQCInspection.device_id == device.id)
    )).scalar_one_or_none()
    current_lot = next((l for l in lots if l.id == device.lot_id), None)
    current_loc = next((l for l in storage_locations if l.id == device.location_id), None)
    return templates.TemplateResponse("devices/edit.html", {
        "request": request, "current_user": current_user,
        "device": device, "lots": lots,
        "current_lot": current_lot,
        "storage_locations": storage_locations,
        "zone_labels": ZONE_LABELS,
        "current_loc": current_loc,
        "movements": movements,
        "iqc_inspection": iqc_inspection,
        "success": request.query_params.get("success"),
    })


@router.post("/{barcode}/edit")
@_device_edit_boundary
async def device_edit_save(
    barcode: str,
    request: Request,
    lot_id: str = Form(""),
    sub_category: str = Form(""),
    brand: str = Form(""),
    model: str = Form(""),
    device_type: str = Form(""),
    serial_no: str = Form(""),
    grn_number: str = Form(""),
    cpu: str = Form(""),
    cpu_make: str = Form(""),
    generation: str = Form(""),
    ram_gb: str = Form(""),
    storage_gb: str = Form(""),
    total_ram_count: str = Form(""),
    total_ram_size: str = Form(""),
    ram_summary: str = Form(""),
    total_hdd_count: str = Form(""),
    total_hdd_size: str = Form(""),
    hdd_summary: str = Form(""),
    storage_type: str = Form(""),
    hdd_capacity_gb: str = Form(""),
    screen_size: str = Form(""),
    battery_health_pct: str = Form(""),
    battery_present: str = Form(""),
    bios_password: str = Form("no"),
    color: str = Form(""),
    grade: str = Form(""),
    floor: str = Form(""),
    warehouse: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    qty: str = Form(""),
    device_price_input: str = Form(""),
    min_selling_price_input: str = Form(""),
    max_selling_price_input: str = Form(""),
    barcode_new: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(edit_allowed),
    _perm: User = Depends(require_additional_perm("edit_devices")),
):
    device = await _get_device_or_404(barcode, db)

    # ── Editable Tag Number (up to 100 chars). Check uniqueness BEFORE mutating
    #    anything, so a clash returns without a partial commit. ────────────────
    new_bc = (barcode_new or "").strip()
    if new_bc and new_bc != device.barcode:
        clash = (await db.execute(
            select(Device.id).where(Device.barcode == new_bc, Device.id != device.id)
        )).scalar_one_or_none()
        if clash:
            import urllib.parse
            return RedirectResponse(
                url=f"/devices/{barcode}/edit?error=Tag+Number+{urllib.parse.quote(new_bc)}+already+exists",
                status_code=302)
        device.barcode = new_bc
        # keep WorkOrder display snapshots consistent with the new tag
        await db.execute(update(WorkOrder).where(WorkOrder.device_id == device.id).values(barcode=new_bc))

    if (lot_id or "").strip():
        device.lot_id = lot_id
    else:
        from utils.lot_helpers import get_or_create_unassigned_lot
        unassigned = await get_or_create_unassigned_lot(db)
        device.lot_id = unassigned.id
    device.sub_category = sub_category or None
    device.brand = brand or None
    device.model = model or None
    device.device_type = device_type or None
    device.serial_no = serial_no or None
    if (grn_number or None) != device.grn_number:
        from services.grn_stocked import remember_safely   # GRN Stocked: keep old GRN's count
        await remember_safely(db, [device.id])
    device.grn_number = grn_number or None
    device.cpu = cpu or None
    device.cpu_make = cpu_make or None
    device.generation = generation or None
    # ram_gb/storage_gb inputs were replaced on the Edit form by the IQC-style
    # RAM/Hard Drive (summary) fields; keep them non-destructive — only overwrite
    # the legacy numeric columns when a value is actually submitted.
    # RAM (GB) / Hard Drive (GB) are on the form again (2026-10-22); "16",
    # "16GB" and "1TB" are all read as GB. Blank clears the value.
    # The form shows whole TB as "1TB" (1000 or 1024 GB); an unchanged box
    # keeps the stored number exactly instead of re-reading it as 1024.
    from utils.device_spec import size_to_gb, gb_text

    def _gb_from_form(posted, current):
        posted = (posted or "").strip()
        if not posted:
            return None
        if current and posted.upper().replace(" ", "") == (gb_text(current) or "").upper():
            return current
        return size_to_gb(posted)
    device.ram_gb = _gb_from_form(ram_gb, device.ram_gb)
    device.storage_gb = _gb_from_form(storage_gb, device.storage_gb)
    # IQC-style hardware fields (plain summed size in RAM / Hard Drive)
    device.total_ram_count = total_ram_count.strip() or None
    device.total_ram_size = total_ram_size.strip() or None
    device.ram_summary = ram_summary.strip() or None
    device.total_hdd_count = total_hdd_count.strip() or None
    device.total_hdd_size = total_hdd_size.strip() or None
    device.hdd_summary = hdd_summary.strip() or None
    # Storage Type / HDD Capacity left the Edit form (2026-10-22) — only
    # changed when a value is actually posted, so a save never wipes them.
    if (storage_type or "").strip():
        device.storage_type = storage_type
    if (hdd_capacity_gb or "").strip().isdigit():
        device.hdd_capacity_gb = int(hdd_capacity_gb)
    device.screen_size = screen_size or None
    device.battery_health_pct = int(battery_health_pct) if (battery_health_pct or "").strip().isdigit() else None
    device.bios_password = (bios_password == "yes")
    device.color = color or None
    if grade:
        try:
            device.grade = DeviceGrade(grade)
        except (ValueError, KeyError):
            device.grade = None
    else:
        device.grade = None
    device.floor = floor or None
    # ── Resolve Location ID -> StorageLocation (Floor/Zone -> Location ID cascade). ──
    # warehouse is legacy free-text; best-effort mirror the resolved location's
    # display_name into it for backward-compat display in older reports/exports.
    resolved_location_id = None
    resolved_warehouse = warehouse or None
    if location_id:
        try:
            uuid_module.UUID(str(location_id))
            loc = (await db.execute(
                select(StorageLocation).where(StorageLocation.id == location_id)
            )).scalar_one_or_none()
            if loc:
                resolved_location_id = loc.id
                resolved_warehouse = loc.display_name
        except ValueError:
            pass
    device.location_id = resolved_location_id
    device.warehouse = resolved_warehouse
    device.notes = notes or None
    if qty:
        try:
            device.qty = int(qty)
        except ValueError:
            pass
    if device_price_input:
        try:
            device.device_price = float(device_price_input)
        except ValueError:
            pass
    if min_selling_price_input.strip():
        try:
            device.min_selling_price = float(min_selling_price_input)
        except ValueError:
            pass
    else:
        device.min_selling_price = None
    if max_selling_price_input.strip():
        try:
            device.max_selling_price = float(max_selling_price_input)
        except ValueError:
            pass
    else:
        device.max_selling_price = None
    device.updated_at = app_now()

    # ── IQC condition fields → update (or create) the device's IQC inspection. ──
    # The Edit Device form mirrors the IQC Entry form, so all condition / panel /
    # port fields are submitted here and persisted on IQCInspection (Device holds
    # the hardware-spec fields handled above).
    iqc_inspection = (await db.execute(
        select(IQCInspection).where(IQCInspection.device_id == device.id)
    )).scalar_one_or_none()
    if iqc_inspection is None:
        iqc_inspection = IQCInspection(device_id=device.id)
        db.add(iqc_inspection)
    _form = await request.form()
    _IQC_STR_FIELDS = [
        "battery_present", "battery_cable", "charging_port", "power_on", "status", "all_ok", "r2v3_grade_category",
        "keyboard_working", "touchpad_working", "port_hdmi", "port_usb_working", "port_audio_jack",
        "speaker_status", "wifi_status", "webcam_status", "hdd_connector", "hdd_casing", "dvd_drive",
        "screen_dot", "screen_line", "screen_discoloration", "screen_patch",
        "screen_broken", "screen_flickering", "screen_scratch", "screen_loose", "screen_missing",
        "screen_hinge_broken", "screen_colour_spread", "screen_keyboard_mark",
        "panel_a_scratch", "panel_a_broken", "panel_a_missing", "panel_a_dent", "panel_a_colour_fade",
        "panel_b_scratch", "panel_b_colour_fade", "panel_b_rubber_cut", "panel_b_broken", "panel_b_missing",
        "panel_c_scratch", "panel_c_broken", "panel_c_missing", "panel_c_dent", "panel_c_colour_fade",
        "panel_d_dent", "panel_d_colour_fade", "panel_d_scratch", "panel_d_broken", "panel_d_missing",
        "keyboard_colour_fade", "keyboard_key_missing", "keyboard_hard_press",
        "touchpad_click_working", "touchpad_scratch", "touchpad_colour_fade", "touchpad_missing",
        "cover_ram", "cover_dvd", "cover_storage",
        "hinge_condition", "hinge_cover", "touchpad_logicboard", "fan_working",
    ]
    _IQC_INT_FIELDS = ["usb_a_ports", "usb_c_ports", "ethernet_ports", "storage_health_pct", "fan_sound_dba"]
    for _f in _IQC_STR_FIELDS:
        if _f in _form:
            setattr(iqc_inspection, _f, (_form.get(_f) or None))
    for _f in _IQC_INT_FIELDS:
        if _f in _form:
            _v = (_form.get(_f) or "").strip()
            try:
                setattr(iqc_inspection, _f, int(_v) if _v else None)
            except (ValueError, TypeError):
                setattr(iqc_inspection, _f, None)

    await db.commit()
    return RedirectResponse(
        url=f"/devices/{device.barcode}?success=Device+updated+successfully",
        status_code=302
    )


# ── Quick spec edit from the pipeline pages (2026-10-26) ─────────────────────
# L1/L2, Stress Test and Final QC show a pencil next to the Tag Number that
# opens a small modal for Serial Number / CPU / CPU Gen / Make / Model / RAM /
# Hard Drive. Gated (auth/route_actions.py) on Edit for ANY of those three
# modules, so the engineers on those pages can fix specs without the full
# Device Edit (admin / inventory manager only).
SPEC_EDIT_MODULES = ("repair_l1", "qc_check", "cosmetic_finalqc")


@router.post("/{barcode}/specs")
async def device_specs_quick_save(
    barcode: str,
    request: Request,
    serial_no: str = Form(""),
    cpu: str = Form(""),
    generation: str = Form(""),
    brand: str = Form(""),
    model: str = Form(""),
    ram: str = Form(""),
    hard_drive: str = Form(""),
    grade: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from models.role_permissions import has_any_perm
    from services.audit_engine import audit
    from utils.device_spec import size_to_gb, gb_text, gb_text_spaced, respace_summary

    role = getattr(current_user.role, "value", current_user.role)
    if not has_any_perm(role, SPEC_EDIT_MODULES, "edit"):
        return JSONResponse({"ok": False, "error": "Your role does not have Edit permission here."}, status_code=403)
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        return JSONResponse({"ok": False, "error": "Tag not found"}, status_code=404)

    def _size(posted, current, label):
        posted = (posted or "").strip()
        if not posted:
            return None
        # An unchanged box ("1TB" shown for 1000 or 1024) keeps the stored number.
        if current and posted.upper().replace(" ", "") == (gb_text(current) or "").upper():
            return current
        n = size_to_gb(posted)
        if n is None:
            raise ValueError(f"{label}: enter a size like 16, 16GB or 1TB")
        return n

    try:
        new_ram = _size(ram, device.ram_gb, "RAM")
        new_hdd = _size(hard_drive, device.storage_gb, "Hard Drive")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    new = {
        "serial_no": serial_no.strip() or None, "cpu": cpu.strip() or None,
        "generation": generation.strip() or None, "brand": brand.strip() or None,
        "model": model.strip() or None, "ram_gb": new_ram, "storage_gb": new_hdd,
    }
    # RAM / Hard Drive text copies (2026-10-27): "16GB" and "16 GB" both save
    # as "16 GB" (with a space; whole TB as "1 TB"). Exports and the L1/L2
    # list read these text fields first, so they follow the edit too. The
    # Hard Drive summary keeps a drive-type word after the size ("256 GB SSD").
    # Detailed summaries ("16GB_DDR4_2300MHz_Samsung, 8GB_...") are left
    # as they are — rewriting them would lose the stick / drive details.
    def _simple(text):
        return not text or ("_" not in text and "," not in text)
    if new_ram:
        new["total_ram_size"] = gb_text_spaced(new_ram)
        if _simple(device.ram_summary):
            new["ram_summary"] = gb_text_spaced(new_ram)
    if new_hdd:
        new["total_hdd_size"] = gb_text_spaced(new_hdd)
        if _simple(device.hdd_summary):
            new["hdd_summary"] = respace_summary(device.hdd_summary, new_hdd)
    # Grade (2026-10-28): only when the form has the Grade box (Stress Test
    # page). Blank = no grade; an unreadable value is refused, never guessed.
    if grade is not None:
        from utils.grades import parse_grade
        g = parse_grade(grade) if grade.strip() else None
        if grade.strip() and g is None:
            return JSONResponse({"ok": False, "error": f"Grade: '{grade}' is not a grade"}, status_code=400)
        new["grade"] = g
    old = {k: getattr(device, k) for k in new}
    changed = {k: v for k, v in new.items() if v != old[k]}
    if not changed:
        return JSONResponse({"ok": True, "changed": 0, "message": "No changes"})
    for k, v in changed.items():
        setattr(device, k, v)
    await audit(db, user=current_user, action="DEVICE_SPECS_QUICK_EDIT",
                table_name="devices", record_id=str(device.id),
                old_value={"barcode": device.barcode, **{k: getattr(old[k], "value", old[k]) for k in changed}},
                new_value={"barcode": device.barcode, **{k: getattr(v, "value", v) for k, v in changed.items()}},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "changed": len(changed)})
