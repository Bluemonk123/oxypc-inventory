import uuid
from urllib.parse import quote_plus
from datetime import datetime
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from sqlalchemy.exc import IntegrityError

from templates_config import templates
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, DeviceGrade, STAGE_LABELS
from models.lot import Lot
from models.bucket import Bucket
from models.location import StorageLocation, ZONE_LABELS, DeviceLocationLog, LocationAction
from models.sales import Sale
from models.stock_transfer import StockTransfer
from models.scrap_for_sale import ScrapForSale
from models.work_order import WorkOrder
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm
from models.master import MasterData
from services.audit_engine import audit
from services.notifications import create_notification
from utils.warranty import warranty_status_for_sale
from utils.master_data import entity_values

router = APIRouter(tags=["transfers"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager,
                         UserRole.qc_inspector, UserRole.sales_manager)

FALLBACK_WAREHOUSES = [
    "TRC 1st Floor",
    "TRC 2nd Floor",
    "TRC 3rd Floor",
    "Bluemonk House Showroom",
    "Bluemonk Showroom",
    "Other",
]

DEPARTMENTS = [
    "IQC Handler",
    "L1 Engineer",
    "L2 Engineer",
    "L3 Engineer",
    "QC Handler",
    "Inventory Manager",
    "Sales Manager",
    "Parts Manager",
]

# Departments that map to a repair stage + create a WorkOrder when a user is assigned
# All repair assignment lands in the merged L1/L2 queue (/repair/l1). The l2/l3 stages are
# retired and have no page, so mapping a department to them would strand the device.
DEPT_TO_STAGE = {"L1 Engineer": "l1", "L2 Engineer": "l1", "L3 Engineer": "l1"}
DEPT_TO_ROLE = {
    "IQC Handler": "iqc_inspector", "L1 Engineer": "l1_engineer",
    "L2 Engineer": "l2_engineer", "L3 Engineer": "l3_engineer",
    "QC Handler": "qc_inspector", "Inventory Manager": "inventory_manager",
    "Sales Manager": "sales_manager", "Parts Manager": "spare_parts_manager",
}
STAGE_ENUM = {"l1": DeviceStage.l1}


async def _next_scrap_id(db: AsyncSession) -> str:
    """Autogenerate the next 4-digit Scrap for Sale ID (e.g. "0001"), one per
    Transfer submission with Transfer Type = Scrap for Sale."""
    last = (await db.execute(
        select(ScrapForSale.scrap_id).order_by(ScrapForSale.scrap_id.desc()).limit(1)
    )).scalar()
    n = (int(last) + 1) if last and last.isdigit() else 1
    return f"{n:04d}"


# "Ready for Sale" is now called "Finish Good" in the UI (2026-10-07). The stored /
# posted value stays "ready_for_sale" (older rows also hold "Ready to Sale"), and a
# posted "Finish Good" must do exactly what "ready_for_sale" always did — so every
# spelling is folded to the one canonical value before any logic looks at it.
_FINISH_GOOD_ALIASES = {"ready_for_sale", "ready for sale", "ready to sale", "finish good", "finish_good"}
# every spelling that can be stored on a StockTransfer row, for the list filter
_FINISH_GOOD_STORED = ["ready_for_sale", "Ready for Sale", "Ready to Sale", "Finish Good", "finish_good"]


def canonical_transfer_type(value) -> str:
    v = (value or "").strip()
    return "ready_for_sale" if v.lower() in _FINISH_GOOD_ALIASES else v


def transfer_type_label(value) -> str:
    """Text for the Type column / export: Ready for Sale (any spelling) shows as
    "Finish Good"; every other type keeps its existing wording."""
    if canonical_transfer_type(value) == "ready_for_sale":
        return "Finish Good"
    return (value or "").replace("_", " ").title()


async def _maybe_create_scrap_batch(db: AsyncSession, transfer_type: str, assigned_user, current_user):
    """When Transfer Type == Scrap for Sale, one new ScrapForSale row is
    created per submission (not per device/part) — every StockTransfer row
    this submission creates links back to it via scrap_for_sale_id, and that
    linkage is how the Scrap for Sale page groups its Device Type/Parts
    counts. Returns None for every other transfer_type."""
    if transfer_type != "scrap_for_sale":
        return None
    scrap = ScrapForSale(
        scrap_id=await _next_scrap_id(db),
        assigned_to_user_id=assigned_user.id if assigned_user else None,
        created_by=current_user.username,
    )
    db.add(scrap)
    await db.flush()
    return scrap


async def _gen_work_id(db: AsyncSession) -> str:
    """Generate a unique 12-digit numeric WorkID."""
    base = (await db.execute(select(func.count(WorkOrder.id)))).scalar() or 0
    n = base + 1
    for _ in range(10000):
        wid = str(n).zfill(12)
        taken = (await db.execute(
            select(WorkOrder.id).where(WorkOrder.work_id == wid)
        )).scalar_one_or_none()
        if not taken:
            return wid
        n += 1
    return str(n).zfill(12)


async def _create_work_order_with_unique_id(db: AsyncSession, **fields) -> WorkOrder:
    """Generate a plain numeric WorkID and insert the WorkOrder, retrying
    under a SAVEPOINT if a concurrent request already claimed the ID in
    between _gen_work_id's existence check and this insert — same race and
    same fix as routers/repair.py's _create_work_order_with_unique_id
    (found 2026-09-23 for L3/L4 first; _gen_work_id here has the identical
    count-then-check flaw for Stock Transfer assignment, so two employees
    assigned within the same window could be given the same WorkID). The
    SAVEPOINT keeps the retry scoped to just the insert — it does not roll
    back the caller's earlier transfer/device-move work already pending in
    the same session."""
    for _attempt in range(20):
        work_id = await _gen_work_id(db)
        wo = WorkOrder(work_id=work_id, **fields)
        try:
            async with db.begin_nested():
                db.add(wo)
                await db.flush()
            return wo
        except IntegrityError:
            continue
    raise HTTPException(500, "Could not allocate a unique WorkID after 20 attempts")


async def _resolve_assigned_user(db: AsyncSession, assigned_user_id: str):
    """Resolve the 'Assign To Employee' selection to a User, or None."""
    if not assigned_user_id:
        return None
    try:
        uid = uuid.UUID(assigned_user_id)
    except Exception:
        return None
    return (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()


def _resolve_location_uuid(to_location_id: str):
    """Parse the 'Location ID' dropdown selection into a UUID, or None."""
    if not to_location_id:
        return None
    try:
        return uuid.UUID(to_location_id)
    except Exception:
        return None


async def _engineers_by_role(db: AsyncSession) -> dict:
    """{role_value: [{id, name, username}]} for active L1/L2/L3 engineers."""
    rows = (await db.execute(
        select(User).where(
            User.role.in_([UserRole.l1_engineer, UserRole.l2_engineer, UserRole.l3_engineer]),
            User.status == True,
        ).order_by(User.full_name)
    )).scalars().all()
    out = {"l1_engineer": [], "l2_engineer": [], "l3_engineer": []}
    for u in rows:
        out.setdefault(u.role.value, []).append(
            {"id": str(u.id), "name": u.full_name or u.username, "username": u.username}
        )
    return out


async def _user_display_name_map(db: AsyncSession, raw_values) -> dict:
    """{raw_value: display_name} for a set of transferred_by/received_by
    values, which may be a username (the common case — create_transfer()
    defaults transferred_by to current_user.username, and the form's hidden
    field pre-fills the same) OR already a full name/free text (e.g.
    create_parts_transfer() stores assigned_user.full_name for received_by).
    Only usernames that match an actual User row get remapped; anything else
    (already a name, or a since-deleted username) passes through unchanged
    rather than showing blank."""
    values = {v for v in raw_values if v}
    if not values:
        return {}
    rows = (await db.execute(
        select(User.username, User.full_name).where(User.username.in_(values))
    )).all()
    return {uname: (full_name or uname) for uname, full_name in rows}


def _parse_simple_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _transfers_list_filters(q, transfer_type, transferred_by, location_id, date_from, date_to, entity=""):
    """Filter clauses shared by the /transfers page and its CSV export, so
    export can never drift from what the table is showing.

    Always scoped to source='transfers_new' (2026-09-24) — this page is
    meant to be a log of what actually got submitted through /transfers/new,
    not a general device-movement audit trail. Final QC Fail routing,
    bucket/engineer assignment, Stock Validate reassignment and Stock In's
    own bulk transfer all write StockTransfer rows too (for their own audit
    purposes), but none of them happened on this page, so they're excluded
    here rather than shown indistinguishably from a real transfer. Rows from
    before this column existed were backfilled by
    migrate_stock_transfer_source.py using the same notes/transfer_type
    patterns those write sites have always used — see that script."""
    w = [StockTransfer.source == "transfers_new"]
    if q:
        w.append(StockTransfer.barcode.ilike(f"%{q}%"))
    if transfer_type:
        if canonical_transfer_type(transfer_type) == "ready_for_sale":
            w.append(StockTransfer.transfer_type.in_(_FINISH_GOOD_STORED))
        else:
            w.append(StockTransfer.transfer_type == transfer_type)
    if transferred_by:
        w.append(StockTransfer.transferred_by == transferred_by)
    if entity:
        # Device's live Entity (2026-10-26) — the query outer-joins Device.
        w.append(Device.entity == entity)
    if location_id:
        try:
            w.append(StockTransfer.to_location_id == uuid.UUID(location_id))
        except ValueError:
            pass
    d_from = _parse_simple_date(date_from)
    if d_from:
        w.append(StockTransfer.transfer_date >= d_from)
    d_to = _parse_simple_date(date_to)
    if d_to:
        w.append(StockTransfer.transfer_date <= d_to.replace(hour=23, minute=59, second=59))
    return w


async def _transfers_list_rows(db, q, transfer_type, transferred_by, location_id, date_from, date_to, entity=""):
    """StockTransfer rows plus a LIVE-joined Lot Number and current Stage —
    StockTransfer.lot_number/product_stage are denormalized snapshots taken
    once at transfer-creation time, so a device whose lot lookup missed at
    insert (or that has moved stage since the transfer) shows stale/blank
    values forever even though the device has since changed. Prefer the
    live join for both."""
    stmt = (
        select(StockTransfer, Lot.lot_number, Device.current_stage, Device.entity)
        .outerjoin(Device, StockTransfer.device_id == Device.id)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(*_transfers_list_filters(q, transfer_type, transferred_by, location_id, date_from, date_to, entity))
        .order_by(desc(StockTransfer.transfer_date))
    )
    return (await db.execute(stmt)).all()


@router.get("/transfers", response_class=HTMLResponse)
async def list_transfers(
    request: Request,
    q: str = "",
    transfer_type: str = "",
    transferred_by: str = "",
    location_id: str = "",
    date_from: str = "",
    date_to: str = "",
    entity: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = await _transfers_list_rows(db, q, transfer_type, transferred_by, location_id, date_from, date_to, entity)
    transfers = []
    name_map = await _user_display_name_map(
        db, [v for t, *_ in rows for v in (t.transferred_by, t.received_by)])
    for t, live_lot_number, live_stage, live_entity in rows:
        t._display_entity = live_entity or ""
        t._display_lot_number = live_lot_number or t.lot_number or "—"
        t._display_transferred_by = name_map.get(t.transferred_by, t.transferred_by)
        t._display_received_by = name_map.get(t.received_by, t.received_by)
        t._display_stage = STAGE_LABELS.get(live_stage, live_stage.value) if live_stage else (t.product_stage or "—")
        t._display_type = transfer_type_label(t.transfer_type)
        transfers.append(t)

    transferred_by_raw = [r[0] for r in (await db.execute(
        select(StockTransfer.transferred_by).where(
            StockTransfer.transferred_by.isnot(None), StockTransfer.source == "transfers_new")
        .distinct().order_by(StockTransfer.transferred_by)
    )).all() if r[0]]
    filter_name_map = await _user_display_name_map(db, transferred_by_raw)
    # (raw value, display label) — filtering posts the raw stored value,
    # the dropdown just shows it dressed up as a display name.
    transferred_by_options = sorted(
        ((u, filter_name_map.get(u, u)) for u in transferred_by_raw),
        key=lambda pair: pair[1].lower())

    storage_locations = (await db.execute(
        select(StorageLocation).where(StorageLocation.is_active == True)  # noqa: E712
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )).scalars().all()
    location_by_id = {str(loc.id): loc for loc in storage_locations}

    return templates.TemplateResponse("transfers/list.html", {
        "request": request, "transfers": transfers, "q": q,
        "transfer_type": transfer_type, "current_user": current_user,
        "transferred_by": transferred_by, "transferred_by_options": transferred_by_options,
        "location_id": location_id, "storage_locations": storage_locations,
        "location_by_id": location_by_id, "zone_labels": ZONE_LABELS,
        "date_from": date_from, "date_to": date_to,
        "entity": entity, "entity_options": await entity_values(db),
    })


@router.post("/transfers/bulk-delete")
async def bulk_delete_transfers(
    request: Request,
    transfer_id: list[str] = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("transfers", "edit")),
):
    """Transfers list's checkbox-driven Bulk Delete — permanent delete (same
    semantics as Credit Note's own Bulk Delete), audit-logged per row before
    removal so the record survives in audit_logs even though the row itself
    doesn't."""
    ids = []
    for tid in transfer_id:
        try:
            ids.append(uuid.UUID(tid))
        except ValueError:
            continue
    if not ids:
        return RedirectResponse(url="/transfers?error=Select+at+least+one+row", status_code=302)

    rows = (await db.execute(select(StockTransfer).where(StockTransfer.id.in_(ids)))).scalars().all()
    for row in rows:
        await audit(db, user=current_user, action="STOCK_TRANSFER_DELETED",
                    table_name="stock_transfers", record_id=str(row.id),
                    old_value={"barcode": row.barcode, "transfer_type": row.transfer_type,
                               "transfer_date": row.transfer_date.isoformat() if row.transfer_date else None},
                    request=request)
        await db.delete(row)
    await db.commit()

    return RedirectResponse(url=f"/transfers?success=Deleted+{len(rows)}+transfer+record(s)", status_code=302)


@router.get("/transfers/export")
async def export_transfers(
    q: str = "",
    transfer_type: str = "",
    transferred_by: str = "",
    location_id: str = "",
    date_from: str = "",
    date_to: str = "",
    entity: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """CSV of exactly the rows the /transfers table is showing — shares
    _transfers_list_rows with the page so export can't drift from the table."""
    import csv as _csv
    import io as _io
    from datetime import date as _date

    rows = await _transfers_list_rows(db, q, transfer_type, transferred_by, location_id, date_from, date_to, entity)
    storage_locations = (await db.execute(select(StorageLocation))).scalars().all()
    location_by_id = {str(loc.id): loc.unit_id for loc in storage_locations}
    name_map = await _user_display_name_map(
        db, [v for t, *_ in rows for v in (t.transferred_by, t.received_by)])

    # From / To (2026-10-22): Location IDs, not the free-text warehouse names.
    #   From = the tag's location just before this movement — its latest
    #          DeviceLocationLog entry dated before the transfer (a movement
    #          stamps transfer_date first, then logs the new location).
    #   To   = the movement's destination; when it set none, From (unchanged).
    dev_ids = list({t.device_id for t, *_ in rows if t.device_id})
    logs_by_dev: dict = {}
    for i in range(0, len(dev_ids), 20000):
        for did, lid, at in (await db.execute(
            select(DeviceLocationLog.device_id, DeviceLocationLog.location_id, DeviceLocationLog.logged_at)
            .where(DeviceLocationLog.device_id.in_(dev_ids[i:i + 20000]))
            .order_by(DeviceLocationLog.logged_at)
        )).all():
            logs_by_dev.setdefault(did, []).append((at, lid))

    def _loc_before(t):
        before = None
        for at, lid in logs_by_dev.get(t.device_id, ()):
            if at is None or t.transfer_date is None or at >= t.transfer_date:
                break
            before = lid
        return location_by_id.get(str(before), "") if before else ""

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Date", "Location ID", "Type", "Tag Number", "Entity", "Make / Model", "Quantity",
                "Lot", "From", "To", "Dept.", "Transferred By", "Received By", "Stage",
                "Serial Number", "CPU", "GEN", "RAM", "STORAGE"])
    for t, live_lot_number, live_stage, live_entity in rows:
        _from_loc = _loc_before(t)
        _to_loc = (location_by_id.get(str(t.to_location_id), "") if t.to_location_id else "") or _from_loc
        stage_label = STAGE_LABELS.get(live_stage, live_stage.value) if live_stage else (t.product_stage or "")
        w.writerow([
            # Year-first so Excel reads real dates on any PC locale (2026-10-21).
            t.transfer_date.strftime("%Y-%m-%d %H:%M") if t.transfer_date else "",
            location_by_id.get(str(t.to_location_id), "") if t.to_location_id else "",
            transfer_type_label(t.transfer_type),
            t.barcode or "",
            live_entity or "",
            f"{t.make or ''} {t.model or ''}".strip(),
            t.quantity if t.quantity is not None else "",
            live_lot_number or t.lot_number or "",
            _from_loc,
            _to_loc,
            t.department or "",
            name_map.get(t.transferred_by, t.transferred_by) or "",
            name_map.get(t.received_by, t.received_by) or "",
            stage_label,
            t.serial_no or "",
            t.cpu or "",
            t.generation or "",
            t.ram or "",
            t.hdd or "",
        ])
    data = buf.getvalue().encode("utf-8-sig")
    fname = f"transfers_{_date.today().isoformat()}.csv"
    return Response(
        content=data, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/transfers/new", response_class=HTMLResponse)
async def new_transfer_form(
    request: Request,
    barcode: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    device = None
    if barcode:
        result = await db.execute(
            select(Device, Lot.lot_number)
            .join(Lot, Device.lot_id == Lot.id, isouter=True)
            .where(Device.barcode == barcode)
        )
        row = result.first()
        if row:
            device, lot_number = row
            device._lot_number = lot_number
    # Load warehouses from Master Data (category='warehouse', active only)
    wh_result = await db.execute(
        select(MasterData.value)
        .where(MasterData.category == "warehouse", MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )
    warehouses = [r[0] for r in wh_result.all()] or FALLBACK_WAREHOUSES
    all_users = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )).scalars().all()
    storage_locations = (await db.execute(
        select(StorageLocation).where(StorageLocation.is_active == True)  # noqa: E712
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )).scalars().all()
    return templates.TemplateResponse("transfers/form.html", {
        "request": request, "device": device, "barcode": barcode,
        "warehouses": warehouses, "departments": DEPARTMENTS,
        "all_users": all_users,
        "storage_locations": storage_locations, "zone_labels": ZONE_LABELS,
        "current_user": current_user, "error": None,
        "now": app_now(),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Bucket / Lot lookup APIs — power the "Move Bucket" / "Move Lot" tabs
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/transfers/api/bucket-lookup")
async def bucket_lookup(
    unit_id: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Look up a StorageLocation by unit_id (the 'Bucket ID' the user scans/types),
    then summarize the devices currently sitting at that location: count of tag
    numbers and grade (homogeneous grade shown, else 'Mixed')."""
    if not unit_id:
        return JSONResponse({"found": False})
    loc = (await db.execute(
        select(StorageLocation).where(StorageLocation.unit_id.ilike(unit_id.strip()))
    )).scalar_one_or_none()
    if not loc:
        return JSONResponse({"found": False})

    devices = (await db.execute(
        select(Device).where(Device.location_id == loc.id, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()

    grades = {d.grade.value for d in devices if d.grade}
    if len(grades) == 1:
        grade_display = grades.pop()
    elif len(grades) == 0:
        grade_display = "—"
    else:
        grade_display = "Mixed"

    return JSONResponse({
        "found": True,
        "location_id": str(loc.id),
        "unit_id": loc.unit_id,
        "location_type": loc.unit_type_label,
        "zone": loc.zone.value,
        "tag_count": len(devices),
        "grade": grade_display,
        "barcodes": [d.barcode for d in devices],
    })


@router.get("/transfers/api/lot-lookup")
async def lot_lookup(
    lot_number: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Look up a Lot by lot_number and summarize its member devices: dates
    (stock-in / final QC / sold), Model, Grade, and Warranty Status. If devices
    are heterogeneous, Model/Grade fall back to 'Mixed' and Warranty Status
    reflects the most recent sale among lot devices (or 'Mixed' if inconsistent)."""
    if not lot_number:
        return JSONResponse({"found": False})
    lot = (await db.execute(
        select(Lot).where(Lot.lot_number.ilike(lot_number.strip()))
    )).scalar_one_or_none()
    if not lot:
        return JSONResponse({"found": False})

    devices = (await db.execute(
        select(Device).where(Device.lot_id == lot.id, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()

    models = {d.model for d in devices if d.model}
    model_display = models.pop() if len(models) == 1 else ("Mixed" if len(models) > 1 else "—")

    grades = {d.grade.value for d in devices if d.grade}
    grade_display = grades.pop() if len(grades) == 1 else ("Mixed" if len(grades) > 1 else "—")

    # Representative dates: earliest stock-in movement, earliest final_qc movement,
    # most recent sale among this lot's devices.
    device_ids = [d.id for d in devices]
    stock_in_date = None
    final_qc_date = None
    sold_on_date = None
    warranty_display = "—"

    if device_ids:
        stock_in_row = (await db.execute(
            select(func.min(StageMovement.moved_at)).where(
                StageMovement.device_id.in_(device_ids),
                StageMovement.to_stage == DeviceStage.stock_in,
            )
        )).scalar()
        stock_in_date = stock_in_row

        final_qc_row = (await db.execute(
            select(func.min(StageMovement.moved_at)).where(
                StageMovement.device_id.in_(device_ids),
                StageMovement.to_stage == DeviceStage.final_qc,
            )
        )).scalar()
        final_qc_date = final_qc_row

        sales = (await db.execute(
            select(Sale).where(Sale.device_id.in_(device_ids)).order_by(desc(Sale.sold_at))
        )).scalars().all()
        if sales:
            sold_on_date = sales[0].sold_at
            statuses = {warranty_status_for_sale(s) for s in sales}
            warranty_display = statuses.pop() if len(statuses) == 1 else "Mixed"

    return JSONResponse({
        "found": True,
        "lot_id": str(lot.id),
        "lot_number": lot.lot_number,
        "tag_count": len(devices),
        "model": model_display,
        "grade": grade_display,
        "stock_in_date": stock_in_date.strftime("%Y-%m-%d") if stock_in_date else "—",
        "final_qc_date": final_qc_date.strftime("%Y-%m-%d") if final_qc_date else "—",
        "sold_on_date": sold_on_date.strftime("%Y-%m-%d") if sold_on_date else "—",
        "warranty_status": warranty_display,
        "barcodes": [d.barcode for d in devices],
    })



async def _block_for_assignee(db, devices, transfer_type: str, assigned_user, loc, current_user) -> str | None:
    """Floor Movement with Transfer Type Finish Good / As-Is Lot (2026-10-22):
    the moved tags are BLOCKED for the Assigned To user — Ready to Sale shows
    them as Blocked, not Open. Done as a real Block Tags record (one per
    movement) so the hold shows on the Block Tags page and can be released
    there like any other block. Container / zone / box come from the
    destination location (Floor when none). Tags already blocked are left as
    they are. Returns the new Block ID, or None when nothing was blocked.
    Caller commits."""
    if transfer_type not in ("ready_for_sale", "as_is_lot") or not assigned_user:
        return None
    to_block = [d for d in devices if not d.is_blocked]
    if not to_block:
        return None
    from models.block_tags import BlockRecord, BlockRecordItem
    from models.location import UnitType
    from routers.block_tags import _next_block_id
    b_date = app_now().date()
    container = getattr(loc, "unit_type", None) or UnitType.floor
    record = BlockRecord(
        block_id=await _next_block_id(db, b_date, container),
        block_date=b_date,
        container=container,
        box_number=(getattr(loc, "unit_id", None) or None),
        location_zone=getattr(loc, "zone", None),
        block_for_user_id=assigned_user.id,
        customer_name=None,
        created_by_user_id=current_user.id,
    )
    db.add(record)
    await db.flush()
    for d in to_block:
        db.add(BlockRecordItem(
            block_record_id=record.id, device_id=d.id,
            barcode=d.barcode, lot_number=d.sub_lot_number or None,
            model=f"{d.brand or ''} {d.model or ''}".strip(),
            cpu=d.cpu, ram_gb=d.ram_gb, storage_gb=d.storage_gb,
        ))
        d.is_blocked = True
    await audit(db, user=current_user, action="FLOOR_MOVEMENT_BLOCKED",
                table_name="block_records", record_id=record.block_id,
                new_value={"block_id": record.block_id, "transfer_type": transfer_type,
                           "block_for": assigned_user.username, "tags": len(to_block)})
    return record.block_id

@router.post("/transfers/new")
async def create_transfer(
    request: Request,
    barcode: list[str] = Form(...),
    transfer_type: str = Form(...),
    from_warehouse: str = Form(""),
    to_warehouse: str = Form(""),
    transferred_by: str = Form(""),
    received_by: str = Form(""),
    department: str = Form(""),
    transfer_date: str = Form(""),
    assigned_user_id: str = Form(""),
    notes: str = Form(""),
    to_location_id: str = Form(""),
    as_is_lot_choice: str = Form(""),
    sub_lot_value: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("transfers", "add")),
):
    """Move Item tab — scanned barcodes are accumulated client-side into a
    list (see stockScan-style multi-scan JS); one StockTransfer row is
    created per barcode with the same transfer options applied to all.

    as_is_lot_choice/sub_lot_value: only meaningful when transfer_type ==
    "as_is_lot" and as_is_lot_choice == "new_sub_lot" — bulk-writes
    Device.sub_lot_number across every scanned Tag Number below. "Current
    Lot" (the default radio) intentionally does nothing, per spec."""
    transfer_type = canonical_transfer_type(transfer_type)
    apply_sub_lot = (transfer_type == "as_is_lot" and as_is_lot_choice == "new_sub_lot"
                     and sub_lot_value.strip())
    barcodes = [b.strip() for b in barcode if b and b.strip()]
    if not barcodes:
        return RedirectResponse(url="/transfers/new?error=No+tag+numbers+scanned", status_code=302)

    # transfer_date only ever carries a plain "%Y-%m-%d" date — the form's
    # own input is readonly and pre-filled with today's date, no time
    # component, so parsing it always produced midnight. Timestamp precision
    # only ever came from app_now(); use it unconditionally.
    t_date = app_now()

    assign_uid = None
    if assigned_user_id:
        try:
            assign_uid = uuid.UUID(assigned_user_id)
        except Exception:
            assign_uid = None
    assigned_user = (
        (await db.execute(select(User).where(User.id == assign_uid))).scalar_one_or_none()
        if assign_uid else None
    )
    if not assigned_user:
        return RedirectResponse(url="/transfers/new?error=Select+an+employee+to+assign", status_code=302)

    # Scrap for Sale takes only tags Tags Scrapped hands over (2026-10-23):
    # Scrap for Sale stage, Verified, not already in a Scrap ID batch.
    refused = []
    if transfer_type == "scrap_for_sale":
        from services.scrap_flow import split_scrap_transfer, refused_text
        found = (await db.execute(select(Device).where(Device.barcode.in_(barcodes)))).scalars().all()
        ok, refused = await split_scrap_transfer(db, found)
        ok_bc = {d.barcode for d in ok}
        found_bc = {d.barcode for d in found}
        barcodes = [b for b in barcodes if b in ok_bc or b not in found_bc]
        if refused and not ok_bc:
            return RedirectResponse(
                url="/transfers/new?error=" + quote_plus(
                    "Scrap for Sale takes only Verified tags from Tags Scrapped. Refused: " + refused_text(refused)),
                status_code=302)

    scrap_batch = await _maybe_create_scrap_batch(db, transfer_type, assigned_user, current_user)

    loc_uuid = _resolve_location_uuid(to_location_id)
    loc = None
    if loc_uuid:
        loc = (await db.execute(select(StorageLocation).where(StorageLocation.id == loc_uuid))).scalar_one_or_none()
    moved, not_found, work_ids = [], [], []
    assigned_devices = []   # Finish Good / As-Is Lot: blocked for the assignee after the loop
    for bc in barcodes:
        result = await db.execute(
            select(Device, Lot.lot_number)
            .join(Lot, Device.lot_id == Lot.id, isouter=True)
            .where(Device.barcode == bc)
        )
        row = result.first()
        if not row:
            not_found.append(bc)
            continue
        device, lot_number = row
        if apply_sub_lot:
            device.sub_lot_number = sub_lot_value.strip()

        _from_wh = from_warehouse or getattr(device, "warehouse", None) or "—"
        _to_wh = to_warehouse or _from_wh
        # Ready for Sale / As-Is Lot: stamp the Assigned To employee straight
        # onto the device — that's what the Ready to Sale Tag / As-Is Lot
        # tables' own Assigned To columns read. Scrap for Sale instead links
        # this row to scrap_batch (see StockTransfer below).
        if transfer_type in ("ready_for_sale", "as_is_lot"):
            device.assigned_to_user_id = assigned_user.id
            assigned_devices.append(device)

        transfer = StockTransfer(
            device_id=device.id,
            move_kind="device",
            to_location_id=loc_uuid,
            transfer_type=transfer_type,
            scrap_for_sale_id=scrap_batch.id if scrap_batch else None,
            source="transfers_new",
            from_warehouse=_from_wh,
            to_warehouse=_to_wh,
            transferred_by=transferred_by or current_user.username,
            received_by=received_by or assigned_user.full_name or assigned_user.username,
            department=department or None,
            barcode=device.barcode,
            serial_no=device.serial_no,
            make=device.brand,
            model=device.model,
            cpu=getattr(device, "cpu", None),
            generation=getattr(device, "generation", None),
            ram=str(device.ram_gb) + " GB" if device.ram_gb else None,
            hdd=str(device.storage_gb) + " GB" if device.storage_gb else None,
            category=device.sub_category,
            lot_number=lot_number,
            product_stage=device.current_stage.value if device.current_stage else None,
            transfer_date=t_date,
            notes=notes or None,
            created_by=current_user.username,
        )
        # Move the device's own current location too, not just the transfer
        # log — otherwise the tag keeps showing its old Location ID
        # everywhere else in the app (Device Detail, All Inventory) even
        # though this transfer recorded the new one (2026-09-18).
        #
        # Setting Device.location_id alone is NOT enough (2026-09-18 fix,
        # part 2): every page that displays "Location ID" reads it from the
        # device's LATEST DeviceLocationLog row (_build_location_map in
        # routers/devices.py), falling back to Device.location_id only when
        # no log exists at all. A device with prior location history — the
        # normal case — kept showing its old logged location forever, since
        # the log always outranks the raw column. A log row has to be
        # written here too, same as the pickup-placeback flow in
        # routers/inventory_location.py.
        if loc:
            device.location_id = loc.id
            device.warehouse = loc.display_name
            # A new Location ID resets the tag's own Bin Location (2026-10-16)
            # - the old bin belongs to the old location.
            device.bin_location = None
            device.updated_at = app_now()
            db.add(DeviceLocationLog(
                device_id=device.id, location_id=loc.id, action=LocationAction.moved,
                actor_id=current_user.id, actor_name=current_user.full_name,
                notes=f"Moved via Transfer to TRC ({transfer_type})",
            ))
        elif hasattr(device, "warehouse") and to_warehouse:
            device.warehouse = to_warehouse
            device.updated_at = app_now()
        db.add(transfer)
        await db.flush()

        # ── Assignment only: record the device against the chosen employee via a
        #    WorkOrder. No device stage move and no repair-stage logic. ─────────
        u = assigned_user
        wo = await _create_work_order_with_unique_id(
            db, device_id=device.id, barcode=device.barcode,
            stage="asgn", assigned_role=u.role.value if u.role else None,
            assigned_user_id=u.id, assigned_username=u.username,
            assigned_name=u.full_name, status="pending",
            source_transfer_id=transfer.id, created_by=current_user.username,
        )
        work_id = wo.work_id
        work_ids.append(work_id)
        _device_label = f"{device.brand or ''} {device.model or ''}".strip()
        await create_notification(
            db, user_id=u.id, title="Device Assigned to You",
            message=(
                f"{device.barcode}"
                + (f" ({_device_label})" if _device_label else "")
                + f" has been assigned to you (WorkID: {work_id})."
            ),
            notification_type="info",
            barcode=device.barcode, brand=device.brand, model=device.model,
        )
        moved.append(bc)

    block_id = await _block_for_assignee(db, assigned_devices, transfer_type, assigned_user, loc, current_user)

    await audit(db, user=current_user, action="STOCK_TRANSFER",
                table_name="stock_transfers", record_id=None,
                new_value={
                    "barcodes": moved, "transfer_type": transfer_type,
                    "from_warehouse": from_warehouse, "to_warehouse": to_warehouse,
                },
                request=request)
    await db.commit()

    if not moved:
        return RedirectResponse(
            url=f"/transfers/new?error=None+of+the+scanned+tag+numbers+were+found:+{','.join(not_found)}",
            status_code=302)
    msg = f"Moved+{len(moved)}+tag+number(s)"
    if block_id:
        msg += f"+—+blocked+for+the+assignee+(Block+{block_id})"
    if work_ids:
        msg += f"+—+{len(work_ids)}+WorkID(s)+created"
    if not_found:
        msg += f"+({len(not_found)}+not+found)"
    if refused:
        from services.scrap_flow import refused_text
        msg += "+" + quote_plus(f"({len(refused)} refused: {refused_text(refused)})")
    return RedirectResponse(url=f"/transfers?success={msg}", status_code=302)


@router.post("/transfers/new/parts")
async def create_parts_transfer(
    request: Request,
    part_name: str = Form(...),
    quantity: int = Form(...),
    transfer_type: str = Form(...),
    assigned_user_id: str = Form(""),
    transfer_date: str = Form(""),
    notes: str = Form(""),
    to_location_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("transfers", "add")),
):
    """Move Parts tab — a spare-parts stock movement, not tied to a specific
    device. Part Name lands in `model` (the same cell the list table already
    shows as "Make / Model"); quantity in the dedicated `quantity` column."""
    transfer_type = canonical_transfer_type(transfer_type)
    if quantity < 1:
        return RedirectResponse(url="/transfers/new?error=Quantity+must+be+at+least+1", status_code=302)

    t_date = app_now()

    assign_uid = None
    if assigned_user_id:
        try:
            assign_uid = uuid.UUID(assigned_user_id)
        except Exception:
            assign_uid = None
    assigned_user = (
        (await db.execute(select(User).where(User.id == assign_uid))).scalar_one_or_none()
        if assign_uid else None
    )
    if not assigned_user:
        return RedirectResponse(url="/transfers/new?error=Select+an+employee+to+assign", status_code=302)

    scrap_batch = await _maybe_create_scrap_batch(db, transfer_type, assigned_user, current_user)

    transfer = StockTransfer(
        device_id=None,
        move_kind="parts",
        to_location_id=_resolve_location_uuid(to_location_id),
        transfer_type=transfer_type,
        scrap_for_sale_id=scrap_batch.id if scrap_batch else None,
        source="transfers_new",
        from_warehouse="—",
        to_warehouse="—",
        transferred_by=current_user.username,
        received_by=assigned_user.full_name or assigned_user.username,
        model=part_name,
        category="Spare Part",
        quantity=quantity,
        transfer_date=t_date,
        notes=notes or None,
        created_by=current_user.username,
    )
    db.add(transfer)
    await db.flush()

    await create_notification(
        db, user_id=assigned_user.id, title="Parts Assigned to You",
        message=f"{quantity} x {part_name} has been assigned to you.",
        notification_type="info",
    )

    await audit(db, user=current_user, action="STOCK_TRANSFER",
                table_name="stock_transfers", record_id=str(transfer.id),
                new_value={"part_name": part_name, "quantity": quantity,
                           "transfer_type": transfer_type},
                request=request)
    await db.commit()

    return RedirectResponse(
        url=f"/transfers?success=Moved+{quantity}+x+{part_name.replace(' ', '+')}",
        status_code=302)


async def _move_devices_bulk(
    db: AsyncSession, request: Request, current_user: User,
    devices: list, move_kind: str, bucket_id, lot_id, group_label: str,
    transfer_type: str, assigned_user: User, transfer_date: str,
    transferred_by: str, received_by: str, notes: str, loc_uuid=None,
    refused_out: list | None = None,
):
    """Shared bulk-assign logic for Move Bucket / Move Lot tabs — creates one
    StockTransfer row per member device and a WorkOrder recording the device
    against the chosen employee. No device stage move, no repair-stage logic."""
    transfer_type = canonical_transfer_type(transfer_type)
    t_date = app_now()

    loc = None
    if loc_uuid:
        loc = (await db.execute(select(StorageLocation).where(StorageLocation.id == loc_uuid))).scalar_one_or_none()

    # Scrap for Sale: only Verified Tags Scrapped tags (see create_transfer).
    if transfer_type == "scrap_for_sale":
        from services.scrap_flow import split_scrap_transfer
        devices, _refused = await split_scrap_transfer(db, devices)
        if refused_out is not None:
            refused_out.extend(_refused)
        if not devices:
            return []

    scrap_batch = await _maybe_create_scrap_batch(db, transfer_type, assigned_user, current_user)

    moved = []
    assigned_devices = []
    for device in devices:
        lot_number = None
        if getattr(device, "lot_id", None):
            lot_row = (await db.execute(select(Lot.lot_number).where(Lot.id == device.lot_id))).scalar()
            lot_number = lot_row

        if transfer_type in ("ready_for_sale", "as_is_lot"):
            device.assigned_to_user_id = assigned_user.id
            assigned_devices.append(device)

        _from_wh = getattr(device, "warehouse", None) or "—"
        transfer = StockTransfer(
            device_id=device.id,
            move_kind=move_kind,
            bucket_id=bucket_id,
            lot_id=lot_id,
            to_location_id=loc_uuid,
            transfer_type=transfer_type,
            scrap_for_sale_id=scrap_batch.id if scrap_batch else None,
            source="transfers_new",
            from_warehouse=_from_wh,
            to_warehouse=_from_wh,
            transferred_by=transferred_by or current_user.username,
            received_by=received_by or assigned_user.full_name or assigned_user.username,
            department=None,
            barcode=device.barcode,
            serial_no=device.serial_no,
            make=device.brand,
            model=device.model,
            cpu=getattr(device, "cpu", None),
            generation=getattr(device, "generation", None),
            ram=str(device.ram_gb) + " GB" if device.ram_gb else None,
            hdd=str(device.storage_gb) + " GB" if device.storage_gb else None,
            category=device.sub_category,
            lot_number=lot_number,
            product_stage=device.current_stage.value if device.current_stage else None,
            transfer_date=t_date,
            notes=notes or None,
            created_by=current_user.username,
        )
        # Same fix as the Move Item tab (2026-09-18, and part 2 same day):
        # move the device's own current location AND write a
        # DeviceLocationLog row — every "Location ID" display reads the
        # device's latest log entry first, Device.location_id only as a
        # fallback when no log exists (see create_device_transfer's fuller
        # comment above).
        if loc:
            device.location_id = loc.id
            device.warehouse = loc.display_name
            # A new Location ID resets the tag's own Bin Location (2026-10-16)
            # - the old bin belongs to the old location.
            device.bin_location = None
            device.updated_at = app_now()
            db.add(DeviceLocationLog(
                device_id=device.id, location_id=loc.id, action=LocationAction.moved,
                actor_id=current_user.id, actor_name=current_user.full_name,
                notes=f"Moved via Transfer to TRC ({move_kind}, {transfer_type})",
            ))
        db.add(transfer)
        await db.flush()

        wo = await _create_work_order_with_unique_id(
            db, device_id=device.id, barcode=device.barcode,
            stage="asgn", assigned_role=assigned_user.role.value if assigned_user.role else None,
            assigned_user_id=assigned_user.id, assigned_username=assigned_user.username,
            assigned_name=assigned_user.full_name, status="pending",
            source_transfer_id=transfer.id, created_by=current_user.username,
        )
        work_id = wo.work_id
        _device_label = f"{device.brand or ''} {device.model or ''}".strip()
        await create_notification(
            db, user_id=assigned_user.id, title="Device Assigned to You",
            message=(
                f"{device.barcode}"
                + (f" ({_device_label})" if _device_label else "")
                + f" has been assigned to you (WorkID: {work_id})."
            ),
            notification_type="info",
            barcode=device.barcode, brand=device.brand, model=device.model,
        )
        moved.append(device.barcode)

    await _block_for_assignee(db, assigned_devices, transfer_type, assigned_user, loc, current_user)

    await audit(db, user=current_user, action="STOCK_TRANSFER_BULK",
                table_name="stock_transfers", record_id=None,
                new_value={
                    "move_kind": move_kind, "group": group_label,
                    "transfer_type": transfer_type, "device_count": len(moved),
                },
                request=request)
    await db.commit()
    return moved


@router.post("/transfers/new/bucket")
async def create_bucket_transfer(
    request: Request,
    unit_id: list[str] = Form(...),
    transfer_type: str = Form(...),
    assigned_user_id: str = Form(""),
    department: str = Form(""),
    transfer_date: str = Form(""),
    transferred_by: str = Form(""),
    received_by: str = Form(""),
    notes: str = Form(""),
    to_location_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("transfers", "add")),
):
    """Move Bucket tab — one or more scanned bucket/location unit IDs; assigns
    every active device at those locations to the chosen employee."""
    unit_ids = [u.strip() for u in unit_id if u and u.strip()]
    if not unit_ids:
        return RedirectResponse(url="/transfers/new?error=No+bucket+IDs+scanned", status_code=302)

    assigned_user = await _resolve_assigned_user(db, assigned_user_id)
    if not assigned_user:
        return RedirectResponse(url="/transfers/new?error=Select+an+employee+to+assign", status_code=302)

    loc_uuid = _resolve_location_uuid(to_location_id)
    total_moved, not_found, refused = [], [], []
    for uid in unit_ids:
        loc = (await db.execute(
            select(StorageLocation).where(StorageLocation.unit_id.ilike(uid))
        )).scalar_one_or_none()
        if not loc:
            not_found.append(uid)
            continue
        devices = (await db.execute(
            select(Device).where(Device.location_id == loc.id, Device.is_active == True, Device.is_trashed == False)
        )).scalars().all()
        if not devices:
            continue
        moved = await _move_devices_bulk(
            db, request, current_user, devices, "bucket", None, None, uid,
            transfer_type, assigned_user, transfer_date, transferred_by, received_by,
            notes, loc_uuid, refused_out=refused,
        )
        total_moved.extend(moved)

    from services.scrap_flow import refused_text
    if not total_moved:
        if refused:
            return RedirectResponse(url="/transfers/new?error=" + quote_plus(
                "Scrap for Sale takes only Verified tags from Tags Scrapped. Refused: " + refused_text(refused)),
                status_code=302)
        return RedirectResponse(
            url=f"/transfers/new?error=No+active+devices+found+for+bucket(s):+{','.join(unit_ids)}",
            status_code=302)
    msg = f"Moved+{len(total_moved)}+device(s)+from+{len(unit_ids) - len(not_found)}+bucket(s)"
    if not_found:
        msg += f"+({len(not_found)}+bucket(s)+not+found)"
    if refused:
        msg += "+" + quote_plus(f"({len(refused)} refused: {refused_text(refused)})")
    return RedirectResponse(url=f"/transfers?success={msg}", status_code=302)


@router.post("/transfers/new/lot")
async def create_lot_transfer(
    request: Request,
    lot_number: list[str] = Form(...),
    transfer_type: str = Form(...),
    assigned_user_id: str = Form(""),
    department: str = Form(""),
    transfer_date: str = Form(""),
    transferred_by: str = Form(""),
    received_by: str = Form(""),
    notes: str = Form(""),
    to_location_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("transfers", "add")),
):
    """Move Lot tab — one or more scanned lot numbers; assigns every active
    member device of each lot to the chosen employee."""
    lot_numbers = [n.strip() for n in lot_number if n and n.strip()]
    if not lot_numbers:
        return RedirectResponse(url="/transfers/new?error=No+lot+numbers+scanned", status_code=302)

    assigned_user = await _resolve_assigned_user(db, assigned_user_id)
    if not assigned_user:
        return RedirectResponse(url="/transfers/new?error=Select+an+employee+to+assign", status_code=302)

    loc_uuid = _resolve_location_uuid(to_location_id)
    total_moved, not_found, refused = [], [], []
    for ln in lot_numbers:
        lot = (await db.execute(
            select(Lot).where(Lot.lot_number.ilike(ln))
        )).scalar_one_or_none()
        if not lot:
            not_found.append(ln)
            continue
        devices = (await db.execute(
            select(Device).where(Device.lot_id == lot.id, Device.is_active == True, Device.is_trashed == False)
        )).scalars().all()
        if not devices:
            continue
        moved = await _move_devices_bulk(
            db, request, current_user, devices, "lot", None, lot.id, ln,
            transfer_type, assigned_user, transfer_date, transferred_by, received_by,
            notes, loc_uuid, refused_out=refused,
        )
        total_moved.extend(moved)

    from services.scrap_flow import refused_text
    if not total_moved:
        if refused:
            return RedirectResponse(url="/transfers/new?error=" + quote_plus(
                "Scrap for Sale takes only Verified tags from Tags Scrapped. Refused: " + refused_text(refused)),
                status_code=302)
        return RedirectResponse(
            url=f"/transfers/new?error=No+active+devices+found+for+lot(s):+{','.join(lot_numbers)}",
            status_code=302)
    msg = f"Moved+{len(total_moved)}+device(s)+from+{len(lot_numbers) - len(not_found)}+lot(s)"
    if not_found:
        msg += f"+({len(not_found)}+lot(s)+not+found)"
    if refused:
        msg += "+" + quote_plus(f"({len(refused)} refused: {refused_text(refused)})")
    return RedirectResponse(url=f"/transfers?success={msg}", status_code=302)


@router.get("/transfers/{transfer_id}", response_class=HTMLResponse)
async def transfer_detail(
    transfer_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(StockTransfer).where(StockTransfer.id == transfer_id))
    transfer = result.scalar_one_or_none()
    if not transfer:
        raise HTTPException(404)
    return templates.TemplateResponse("transfers/detail.html", {
        "request": request, "transfer": transfer, "current_user": current_user,
    })
