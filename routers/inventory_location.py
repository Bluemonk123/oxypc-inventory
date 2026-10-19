"""
Inventory Location Tracking
============================
Covers:
  - Storage location master (CRUD)
  - Device pick-up / place-back / move
  - Live location dashboard (what's where)
  - End-of-day gap alert list
  - Monthly physical audit (initiate, scan, close, report)
"""

from datetime import datetime, timedelta
from utils.timezone import app_now
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, desc

from database import get_db
from templates_config import templates
from auth.dependencies import get_current_user, require_roles, verify_csrf
from models.user import User, UserRole
from models.device import Device, DeviceStage
from models.location import (
    StorageLocation, DeviceLocationLog, InventoryAudit, AuditScanItem,
    LocationAction, AuditStatus, ScanStatus, ZoneType, UnitType,
    ZONE_LABELS, UNIT_TYPE_LABELS,
)
from models.audit_record import AuditRecord, AuditRecordItem
from models.bucket import Bucket
from models.stock_transfer import StockTransfer
from models.master import MasterData
from sqlalchemy.exc import IntegrityError


# Fallback when the "location_zone" Master Data category is empty (nothing
# seeded yet, or every row deactivated) — the exact curated list the Add/Edit
# Location modal has always shown, so that required dropdown is never left
# empty. _zone_dropdown_options() below is the normal path once that
# category has rows.
_DEFAULT_ZONE_DROPDOWN_OPTIONS = [
    ZoneType.workshop, ZoneType.holding, ZoneType.dispatch, ZoneType.showroom, ZoneType.warehouse,
]


async def _zone_dropdown_options(db: AsyncSession) -> list:
    """Zones offered in the Add/Edit Location modal — sourced from the
    admin-editable Master Data "location_zone" category (Admin > Master Data
    > Dropdown Configuration > Storage Locations: Zone) instead of a
    hardcoded list, so which zones are offered there — and their order — is
    no longer a code change.

    StorageLocation.zone is still a strongly-typed Postgres enum column
    underneath (ZoneType), and every other place that reads it (Dashboard
    filter, All Inventory export, Stock Inward) still keys off that same
    enum + the existing ZONE_LABELS display text — none of that changed. So
    each Master Data row's `value` must itself be a valid ZoneType code
    (e.g. "workshop", not "TRC Floor") for a location saved through it to
    keep working everywhere else. Rows that don't parse to a known ZoneType
    are skipped rather than crashing the page (a bad admin edit shouldn't
    break Location Master), and if nothing parses at all this falls back to
    _DEFAULT_ZONE_DROPDOWN_OPTIONS."""
    rows = (await db.execute(
        select(MasterData.value)
        .where(MasterData.category == "location_zone", MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.created_at)
    )).scalars().all()
    options = []
    for raw in rows:
        try:
            options.append(ZoneType(raw))
        except ValueError:
            continue
    return options or _DEFAULT_ZONE_DROPDOWN_OPTIONS

router = APIRouter(prefix="/locations", tags=["locations"], dependencies=[Depends(verify_csrf)])

_ALL_ROLES = (
    UserRole.admin, UserRole.inventory_manager, UserRole.iqc_inspector,
    UserRole.l1_engineer, UserRole.l2_engineer, UserRole.l3_engineer,
    UserRole.qc_inspector, UserRole.sales, UserRole.sales_manager,
    UserRole.spare_parts_manager,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_device_current_location(db: AsyncSession, device_id) -> Optional[DeviceLocationLog]:
    """Return the most-recent DeviceLocationLog row for this device."""
    result = await db.execute(
        select(DeviceLocationLog)
        .where(DeviceLocationLog.device_id == device_id)
        .order_by(desc(DeviceLocationLog.logged_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _devices_in_hand(db: AsyncSession):
    """
    Return all device-ids whose last location log action is 'picked_up'
    (meaning they haven't been placed back yet).
    """
    # Subquery: latest log per device
    sub = (
        select(
            DeviceLocationLog.device_id,
            func.max(DeviceLocationLog.logged_at).label("latest")
        )
        .group_by(DeviceLocationLog.device_id)
        .subquery()
    )
    result = await db.execute(
        select(DeviceLocationLog)
        .join(sub, and_(
            DeviceLocationLog.device_id == sub.c.device_id,
            DeviceLocationLog.logged_at == sub.c.latest,
        ))
        .join(Device, Device.id == DeviceLocationLog.device_id)
        .where(DeviceLocationLog.action == LocationAction.picked_up, Device.is_trashed == False)
    )
    return result.scalars().all()


async def _gap_devices(db: AsyncSession, hours: int = 24):
    """
    Devices considered 'location gaps':
      1. Last log = picked_up (not returned)
      2. Active device with NO location log at all
    Excludes sold / scrapped devices.
    """
    active_stages = [
        DeviceStage.iqc, DeviceStage.stock_in,
        DeviceStage.l1, DeviceStage.l2, DeviceStage.l3,
        DeviceStage.qc_check, DeviceStage.cosmetic_received, DeviceStage.cleaning, DeviceStage.dry_sanding,
        DeviceStage.masking, DeviceStage.painting, DeviceStage.water_sanding, DeviceStage.cosmetic_completed,
        DeviceStage.final_qc, DeviceStage.ready_to_sale,
    ]
    # 1. Picked-up and not returned
    in_hand = await _devices_in_hand(db)
    in_hand_ids = {log.device_id for log in in_hand}

    # 2. Active devices that have never had a location log
    logged_ids_result = await db.execute(
        select(DeviceLocationLog.device_id).distinct()
    )
    logged_ids = set(logged_ids_result.scalars().all())

    all_active_result = await db.execute(
        select(Device.id).where(Device.is_trashed == False, Device.current_stage.in_(active_stages))
    )
    all_active_ids = set(all_active_result.scalars().all())

    never_logged_ids = all_active_ids - logged_ids

    gap_ids = in_hand_ids | never_logged_ids
    return gap_ids, in_hand, never_logged_ids


# ─────────────────────────────────────────────────────────────────────────────
#  1. Storage Location Master — Admin / Inventory Manager only
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/master", response_class=HTMLResponse)
async def location_master(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    result = await db.execute(
        select(StorageLocation).order_by(StorageLocation.zone, StorageLocation.unit_id)
    )
    locations = result.scalars().all()

    # Count devices per location (latest log = placed_back or assigned or moved).
    # Single query for all locations at once (was previously one query per
    # location, each re-scanning the whole DeviceLocationLog table — O(N)
    # round-trips that got slower as more locations/logs were added).
    loc_counts = {str(loc.id): 0 for loc in locations}
    if locations:
        latest_sub = (
            select(
                DeviceLocationLog.device_id,
                func.max(DeviceLocationLog.logged_at).label("latest")
            )
            .group_by(DeviceLocationLog.device_id)
            .subquery()
        )
        count_rows = (await db.execute(
            select(DeviceLocationLog.location_id, func.count().label("cnt"))
            .join(latest_sub, and_(
                DeviceLocationLog.device_id == latest_sub.c.device_id,
                DeviceLocationLog.logged_at == latest_sub.c.latest,
            ))
            .where(
                DeviceLocationLog.location_id.in_([loc.id for loc in locations]),
                DeviceLocationLog.action.in_([
                    LocationAction.assigned,
                    LocationAction.placed_back,
                    LocationAction.moved,
                ])
            )
            .group_by(DeviceLocationLog.location_id)
        )).all()
        for location_id, cnt in count_rows:
            loc_counts[str(location_id)] = cnt

    return templates.TemplateResponse("location/master.html", {
        "request": request,
        "current_user": current_user,
        "locations": locations,
        "loc_counts": loc_counts,
        "zone_types": list(ZoneType),
        "unit_types": list(UnitType),
        "zone_labels": ZONE_LABELS,
        "unit_type_labels": UNIT_TYPE_LABELS,
        "add_location_zone_types": await _zone_dropdown_options(db),
    })


@router.get("/master/bulk-template")
async def location_bulk_template(
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    import csv, io
    from fastapi.responses import StreamingResponse
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["zone", "unit_type", "unit_id", "slot", "description", "capacity"])
    writer.writerow(["workshop", "rack", "RACK-A1", "1", "Example rack", "50"])
    output.seek(0)
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=location_template.csv"},
    )


@router.post("/master/bulk-upload")
async def location_bulk_upload(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    import csv, io

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    added, skipped, errors = 0, 0, []
    existing_unit_ids = set((await db.execute(select(StorageLocation.unit_id))).scalars().all())

    zone_values = {z.value for z in ZoneType}
    unit_type_values = {u.value for u in UnitType}

    for i, row in enumerate(reader, start=2):
        unit_id = (row.get("unit_id") or "").strip()
        zone = (row.get("zone") or "").strip().lower()
        unit_type = (row.get("unit_type") or "").strip().lower()
        if not unit_id:
            errors.append(f"Row {i}: unit_id is required")
            continue
        if unit_id in existing_unit_ids:
            skipped += 1
            continue
        if zone not in zone_values:
            errors.append(f"Row {i}: invalid zone '{row.get('zone')}'")
            continue
        if unit_type not in unit_type_values:
            errors.append(f"Row {i}: invalid unit_type '{row.get('unit_type')}'")
            continue
        capacity_raw = (row.get("capacity") or "").strip()
        loc = StorageLocation(
            zone=ZoneType(zone), unit_type=UnitType(unit_type), unit_id=unit_id,
            slot=(row.get("slot") or "").strip() or None,
            description=(row.get("description") or "").strip() or None,
            capacity=int(capacity_raw) if capacity_raw.isdigit() else None,
        )
        db.add(loc)
        existing_unit_ids.add(unit_id)
        added += 1

    await db.commit()
    msg = f"{added}+location(s)+added,+{skipped}+skipped"
    if errors:
        msg += f"&error={'; '.join(errors[:10])}"
        if len(errors) > 10:
            msg += "…"
    return RedirectResponse(url=f"/locations/master?success={msg}", status_code=302)


@router.post("/master/create", response_class=HTMLResponse)
async def create_location(
    request: Request,
    zone: str = Form(...),
    unit_type: str = Form(...),
    unit_id: str = Form(...),
    slot: str = Form(""),
    description: str = Form(""),
    capacity: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    loc = StorageLocation(
        zone=ZoneType(zone),
        unit_type=UnitType(unit_type),
        unit_id=unit_id.strip().upper(),
        slot=slot.strip() or None,
        description=description.strip() or None,
        capacity=int(capacity) if capacity.strip() else None,
    )
    db.add(loc)
    await db.flush()
    return RedirectResponse("/locations/master?success=Location+created", status_code=303)


@router.post("/master/{loc_id}/edit", response_class=HTMLResponse)
async def edit_location(
    loc_id: str,
    zone: str = Form(...),
    unit_type: str = Form(...),
    unit_id: str = Form(...),
    slot: str = Form(""),
    description: str = Form(""),
    capacity: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    result = await db.execute(select(StorageLocation).where(StorageLocation.id == loc_id))
    loc = result.scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404)
    loc.zone = ZoneType(zone)
    loc.unit_type = UnitType(unit_type)
    loc.unit_id = unit_id.strip().upper()
    loc.slot = slot.strip() or None
    loc.description = description.strip() or None
    loc.capacity = int(capacity) if capacity.strip() else None
    await db.flush()
    return RedirectResponse("/locations/master?success=Location+updated", status_code=303)


@router.post("/master/{loc_id}/toggle", response_class=HTMLResponse)
async def toggle_location(
    loc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    result = await db.execute(select(StorageLocation).where(StorageLocation.id == loc_id))
    loc = result.scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404)
    loc.is_active = not loc.is_active
    await db.flush()
    return RedirectResponse("/locations/master?success=Location+updated", status_code=303)


@router.post("/master/{loc_id}/delete", response_class=HTMLResponse)
async def delete_location(
    loc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    """Hard-delete a StorageLocation — only when nothing references it.
    Every FK to storage_locations.id (devices, device_location_log,
    audit_records, audit_record_items x2, audit_scan_item, buckets,
    stock_transfers) is nullable with no ON DELETE rule, so a DELETE on a
    referenced row would otherwise fail with an opaque IntegrityError (or
    worse, silently orphan history if a cascade were ever added later).
    Checked explicitly up front so the error is a clear, specific message —
    "Deactivate" (toggle_location above) is the right tool for a location
    that still has history; this is only for genuinely unused entries."""
    result = await db.execute(select(StorageLocation).where(StorageLocation.id == loc_id))
    loc = result.scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404)
    unit_id = loc.unit_id  # captured now — rollback below would expire `loc`,
                            # and an expired attribute can't be lazily reloaded
                            # on an async session outside an await context.

    reference_checks = (
        (Device, Device.location_id, "device(s)"),
        (DeviceLocationLog, DeviceLocationLog.location_id, "location log entries"),
        (AuditRecord, AuditRecord.system_location_id, "Physical Audit record(s)"),
        (AuditRecordItem, AuditRecordItem.system_location_id, "Physical Audit item(s) (system location)"),
        (AuditRecordItem, AuditRecordItem.my_location_id, "Physical Audit item(s) (my location)"),
        (AuditScanItem, AuditScanItem.location_id, "Zone Audit scan item(s)"),
        (Bucket, Bucket.location_id, "bucket(s)"),
        (StockTransfer, StockTransfer.to_location_id, "transfer record(s)"),
    )
    for model, column, label in reference_checks:
        in_use = (await db.execute(
            select(model.id).where(column == loc_id).limit(1)
        )).scalar_one_or_none()
        if in_use is not None:
            return RedirectResponse(
                f"/locations/master?error=Cannot+delete+{unit_id}+%E2%80%94+still+referenced+by+{label.replace(' ', '+')}.+Deactivate+it+instead.",
                status_code=303,
            )

    try:
        await db.delete(loc)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return RedirectResponse(
            f"/locations/master?error=Cannot+delete+{unit_id}+%E2%80%94+still+referenced+elsewhere.+Deactivate+it+instead.",
            status_code=303,
        )
    return RedirectResponse(f"/locations/master?success={unit_id}+deleted", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
#  2. Live Location Dashboard — all active devices shown by location
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def location_dashboard(
    request: Request,
    zone: str = "",
    unit_type: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # All active locations. "SOLD" is a virtual/convention location some sites
    # use to park sold devices' location records — it isn't a real ZoneType
    # member, just a StorageLocation.unit_id an admin can create, so it's
    # matched by name here rather than a schema field. Hidden unconditionally
    # so it activates automatically whenever such a row exists.
    loc_query = select(StorageLocation).where(StorageLocation.is_active == True)
    if zone:
        loc_query = loc_query.where(StorageLocation.zone == zone)
    if unit_type:
        loc_query = loc_query.where(StorageLocation.unit_type == unit_type)
    loc_result = await db.execute(loc_query.order_by(StorageLocation.zone, StorageLocation.unit_id))
    locations = [loc for loc in loc_result.scalars().all() if (loc.unit_id or "").strip().upper() != "SOLD"]

    # Tag-number lists on this dashboard exclude devices that have left active
    # inventory — Sold, Scrapped, and Scrap for Sale (parts-harvest scrap held
    # for resale, still effectively disposed of for location-tracking purposes).
    _terminal_stages = (DeviceStage.sold, DeviceStage.scrapped, DeviceStage.scrap_for_sale)

    # For each location: get current devices
    # "current" = latest log per device is placed_back / assigned / moved to this location
    sub = (
        select(
            DeviceLocationLog.device_id,
            func.max(DeviceLocationLog.logged_at).label("latest")
        )
        .group_by(DeviceLocationLog.device_id)
        .subquery()
    )
    current_logs_result = await db.execute(
        select(DeviceLocationLog, Device.barcode, Device.brand, Device.model,
               Device.sub_category, Device.current_stage, Device.grade)
        .join(sub, and_(
            DeviceLocationLog.device_id == sub.c.device_id,
            DeviceLocationLog.logged_at == sub.c.latest,
        ))
        .join(Device, DeviceLocationLog.device_id == Device.id)
        .where(
            DeviceLocationLog.action.in_([
                LocationAction.assigned, LocationAction.placed_back, LocationAction.moved
            ]),
            Device.current_stage.notin_(_terminal_stages),
        )
    )
    current_logs = current_logs_result.all()

    # Build location → devices map
    loc_map: dict = {str(loc.id): [] for loc in locations}
    for row in current_logs:
        log, barcode, brand, model, sub_cat, stage, grade = row
        lid = str(log.location_id)
        if lid in loc_map:
            loc_map[lid].append({
                "log": log,
                "barcode": barcode,
                "brand": brand,
                "model": model,
                "sub_category": sub_cat,
                "stage": stage.value if stage else "",
                "grade": grade.value if grade else "",
                "device_id": str(log.device_id),
            })

    # In-hand devices (picked up, not returned)
    in_hand_logs = await _devices_in_hand(db)
    in_hand_detail = []
    for log in in_hand_logs:
        dev_result = await db.execute(
            select(Device).where(Device.id == log.device_id)
        )
        dev = dev_result.scalar_one_or_none()
        if dev and dev.current_stage not in _terminal_stages:
            in_hand_detail.append({"log": log, "device": dev})

    # Gap count for header badge
    gap_ids, _, _ = await _gap_devices(db)

    return templates.TemplateResponse("location/dashboard.html", {
        "request": request,
        "current_user": current_user,
        "locations": locations,
        "loc_map": loc_map,
        "in_hand_detail": in_hand_detail,
        "zone_types": list(ZoneType),
        "zone_labels": ZONE_LABELS,
        "selected_zone": zone,
        "unit_types": list(UnitType),
        "selected_unit_type": unit_type,
        "gap_count": len(gap_ids),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  3. Pick-Up / Place-Back / Assign for a single device
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/device/{device_id}", response_class=HTMLResponse)
async def device_location_page(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dev_result = await db.execute(select(Device).where(Device.id == device_id))
    device = dev_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    current_log = await _get_device_current_location(db, device_id)
    current_loc = None
    if current_log and current_log.location_id:
        loc_result = await db.execute(
            select(StorageLocation).where(StorageLocation.id == current_log.location_id)
        )
        current_loc = loc_result.scalar_one_or_none()

    # All active locations for the dropdown
    locs_result = await db.execute(
        select(StorageLocation)
        .where(StorageLocation.is_active == True)
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )
    all_locs = locs_result.scalars().all()

    # Recent logs (last 20) for this device
    logs_result = await db.execute(
        select(DeviceLocationLog)
        .where(DeviceLocationLog.device_id == device_id)
        .order_by(desc(DeviceLocationLog.logged_at))
        .limit(20)
    )
    logs = logs_result.scalars().all()

    in_hand = current_log and current_log.action == LocationAction.picked_up

    return templates.TemplateResponse("location/device_location.html", {
        "request": request,
        "current_user": current_user,
        "device": device,
        "current_log": current_log,
        "current_loc": current_loc,
        "in_hand": in_hand,
        "all_locs": all_locs,
        "logs": logs,
        "zone_labels": ZONE_LABELS,
    })


@router.post("/device/{device_id}/pickup")
async def pickup_device(
    device_id: str,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dev_result = await db.execute(select(Device).where(Device.id == device_id))
    device = dev_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404)

    log = DeviceLocationLog(
        device_id=device.id,
        location_id=None,
        action=LocationAction.picked_up,
        actor_id=current_user.id,
        actor_name=current_user.full_name,
        notes=notes.strip() or None,
    )
    db.add(log)
    await db.flush()
    return RedirectResponse(
        f"/locations/device/{device_id}?success=Device+picked+up",
        status_code=303
    )


@router.post("/device/{device_id}/placeback")
async def placeback_device(
    device_id: str,
    location_id: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dev_result = await db.execute(select(Device).where(Device.id == device_id))
    device = dev_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404)

    loc_result = await db.execute(
        select(StorageLocation).where(StorageLocation.id == location_id)
    )
    loc = loc_result.scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    # Determine action: first time = assigned, previously had a location = placed_back
    prev_log = await _get_device_current_location(db, device_id)
    if prev_log and prev_log.action == LocationAction.picked_up:
        action = LocationAction.placed_back
    elif prev_log and prev_log.location_id:
        action = LocationAction.moved
    else:
        action = LocationAction.assigned

    log = DeviceLocationLog(
        device_id=device.id,
        location_id=loc.id,
        action=action,
        actor_id=current_user.id,
        actor_name=current_user.full_name,
        notes=notes.strip() or None,
    )
    db.add(log)
    await db.flush()
    return RedirectResponse(
        f"/locations/device/{device_id}?success=Device+location+updated",
        status_code=303
    )


@router.post("/device/{device_id}/assign")
async def assign_device_location(
    device_id: str,
    location_id: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick-assign a location without a prior pick-up step."""
    dev_result = await db.execute(select(Device).where(Device.id == device_id))
    device = dev_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404)

    log = DeviceLocationLog(
        device_id=device.id,
        location_id=location_id,
        action=LocationAction.assigned,
        actor_id=current_user.id,
        actor_name=current_user.full_name,
        notes=notes.strip() or None,
    )
    db.add(log)
    await db.flush()
    return RedirectResponse(
        f"/locations/device/{device_id}?success=Location+assigned",
        status_code=303
    )


# ─────────────────────────────────────────────────────────────────────────────
#  4. Gap Alert Dashboard (End-of-Day unaccounted devices)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/gaps", response_class=HTMLResponse)
async def gap_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    gap_ids, in_hand_logs, never_logged_ids = await _gap_devices(db)

    # Enrich in-hand rows
    in_hand_detail = []
    for log in in_hand_logs:
        dev_result = await db.execute(select(Device).where(Device.id == log.device_id))
        dev = dev_result.scalar_one_or_none()
        if dev:
            hours_ago = (app_now() - log.logged_at).total_seconds() / 3600
            in_hand_detail.append({
                "log": log,
                "device": dev,
                "hours_ago": round(hours_ago, 1),
                "severity": "danger" if hours_ago > 8 else ("warning" if hours_ago > 4 else "info"),
            })
    in_hand_detail.sort(key=lambda x: x["hours_ago"], reverse=True)

    # Enrich never-logged rows
    never_logged_detail = []
    if never_logged_ids:
        nl_result = await db.execute(
            select(Device).where(Device.id.in_(list(never_logged_ids)[:200]))
        )
        never_logged_detail = nl_result.scalars().all()

    return templates.TemplateResponse("location/gaps.html", {
        "request": request,
        "current_user": current_user,
        "in_hand_detail": in_hand_detail,
        "never_logged_detail": never_logged_detail,
        "total_gaps": len(gap_ids),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  5. Physical Inventory Audit
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def audit_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    result = await db.execute(
        select(InventoryAudit).order_by(desc(InventoryAudit.initiated_at))
    )
    audits = result.scalars().all()
    return templates.TemplateResponse("location/audit_list.html", {
        "request": request,
        "current_user": current_user,
        "audits": audits,
        "zone_labels": ZONE_LABELS,
        "zone_types": list(ZoneType),
    })


@router.post("/audit/create")
async def create_audit(
    zone_filter: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    # Generate audit number
    count_result = await db.execute(select(func.count(InventoryAudit.id)))
    seq = (count_result.scalar() or 0) + 1
    now = app_now()
    audit_number = f"AUD-{now.year}-{now.month:02d}-{seq:03d}"

    # Calculate expected count (devices with a known location matching zone)
    sub = (
        select(
            DeviceLocationLog.device_id,
            func.max(DeviceLocationLog.logged_at).label("latest")
        )
        .group_by(DeviceLocationLog.device_id)
        .subquery()
    )
    expected_q = (
        select(func.count().label("cnt"))
        .select_from(
            select(DeviceLocationLog)
            .join(sub, and_(
                DeviceLocationLog.device_id == sub.c.device_id,
                DeviceLocationLog.logged_at == sub.c.latest,
            ))
            .where(
                DeviceLocationLog.action.in_([
                    LocationAction.assigned, LocationAction.placed_back, LocationAction.moved
                ])
            )
            .subquery()
        )
    )
    if zone_filter:
        # join through storage_locations to filter by zone
        expected_q = (
            select(func.count())
            .select_from(
                select(DeviceLocationLog)
                .join(sub, and_(
                    DeviceLocationLog.device_id == sub.c.device_id,
                    DeviceLocationLog.logged_at == sub.c.latest,
                ))
                .join(StorageLocation, DeviceLocationLog.location_id == StorageLocation.id)
                .where(
                    DeviceLocationLog.action.in_([
                        LocationAction.assigned, LocationAction.placed_back, LocationAction.moved
                    ]),
                    StorageLocation.zone == zone_filter,
                )
                .subquery()
            )
        )
    expected_count = (await db.execute(expected_q)).scalar() or 0

    audit = InventoryAudit(
        audit_number=audit_number,
        zone_filter=zone_filter or None,
        status=AuditStatus.in_progress,
        initiated_by=current_user.id,
        initiated_by_name=current_user.full_name,
        notes=notes.strip() or None,
        expected_count=expected_count,
    )
    db.add(audit)
    await db.flush()
    return RedirectResponse(f"/locations/audit/{audit.id}", status_code=303)


@router.get("/audit/{audit_id}", response_class=HTMLResponse)
async def audit_detail(
    audit_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    audit_result = await db.execute(
        select(InventoryAudit).where(InventoryAudit.id == audit_id)
    )
    audit = audit_result.scalar_one_or_none()
    if not audit:
        raise HTTPException(status_code=404)

    scans_result = await db.execute(
        select(AuditScanItem, Device.barcode, Device.brand, Device.model, Device.sub_category)
        .outerjoin(Device, AuditScanItem.device_id == Device.id)
        .where(AuditScanItem.audit_id == audit_id)
        .order_by(desc(AuditScanItem.scanned_at))
    )
    scans = scans_result.all()

    found_count = sum(1 for s in scans if s[0].scan_status == ScanStatus.found)
    extra_count = sum(1 for s in scans if s[0].scan_status == ScanStatus.extra)

    # Expected items (for missing computation): devices with known location in scope
    sub = (
        select(
            DeviceLocationLog.device_id,
            func.max(DeviceLocationLog.logged_at).label("latest")
        )
        .group_by(DeviceLocationLog.device_id)
        .subquery()
    )
    expected_q = (
        select(DeviceLocationLog, Device.barcode, Device.brand, Device.model,
               Device.sub_category, Device.current_stage)
        .join(sub, and_(
            DeviceLocationLog.device_id == sub.c.device_id,
            DeviceLocationLog.logged_at == sub.c.latest,
        ))
        .join(Device, DeviceLocationLog.device_id == Device.id)
        .where(
            DeviceLocationLog.action.in_([
                LocationAction.assigned, LocationAction.placed_back, LocationAction.moved
            ])
        )
    )
    if audit.zone_filter:
        expected_q = expected_q.join(
            StorageLocation, DeviceLocationLog.location_id == StorageLocation.id
        ).where(StorageLocation.zone == audit.zone_filter)

    expected_result = await db.execute(expected_q)
    expected_rows = expected_result.all()

    scanned_barcodes = {s[1] for s in scans if s[0].scan_status in (ScanStatus.found, ScanStatus.extra)}
    missing_rows = [r for r in expected_rows if r[1] not in scanned_barcodes]

    # All active locations for scan form
    locs_result = await db.execute(
        select(StorageLocation)
        .where(StorageLocation.is_active == True)
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )
    all_locs = locs_result.scalars().all()

    return templates.TemplateResponse("location/audit_detail.html", {
        "request": request,
        "current_user": current_user,
        "audit": audit,
        "scans": scans,
        "found_count": found_count,
        "extra_count": extra_count,
        "missing_rows": missing_rows,
        "expected_rows": expected_rows,
        "all_locs": all_locs,
        "zone_labels": ZONE_LABELS,
        "ScanStatus": ScanStatus,
        "AuditStatus": AuditStatus,
    })


@router.post("/audit/{audit_id}/scan")
async def record_scan(
    audit_id: str,
    barcode: str = Form(...),
    location_id: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    audit_result = await db.execute(
        select(InventoryAudit).where(InventoryAudit.id == audit_id)
    )
    audit = audit_result.scalar_one_or_none()
    if not audit or audit.status != AuditStatus.in_progress:
        raise HTTPException(status_code=400, detail="Audit not in progress")

    # Look up device by barcode
    dev_result = await db.execute(
        select(Device).where(Device.barcode == barcode.strip())
    )
    device = dev_result.scalar_one_or_none()

    # Determine if expected at this location
    if device:
        current_log = await _get_device_current_location(db, str(device.id))
        if current_log and current_log.location_id == location_id and \
           current_log.action in (LocationAction.assigned, LocationAction.placed_back, LocationAction.moved):
            scan_status = ScanStatus.found
        else:
            scan_status = ScanStatus.extra
    else:
        scan_status = ScanStatus.extra  # unknown barcode

    scan = AuditScanItem(
        audit_id=audit_id,
        device_id=str(device.id) if device else None,
        barcode_scanned=barcode.strip(),
        location_id=location_id or None,
        scan_status=scan_status,
        scanned_by=current_user.id,
        scanned_by_name=current_user.full_name,
        notes=notes.strip() or None,
    )
    db.add(scan)
    await db.flush()
    return RedirectResponse(
        f"/locations/audit/{audit_id}?success=Barcode+{barcode}+scanned",
        status_code=303
    )


@router.post("/audit/{audit_id}/close")
async def close_audit(
    audit_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.admin, UserRole.inventory_manager)),
):
    audit_result = await db.execute(
        select(InventoryAudit).where(InventoryAudit.id == audit_id)
    )
    audit = audit_result.scalar_one_or_none()
    if not audit:
        raise HTTPException(status_code=404)

    scans_result = await db.execute(
        select(AuditScanItem).where(AuditScanItem.audit_id == audit_id)
    )
    scans = scans_result.scalars().all()

    found_count = sum(1 for s in scans if s.scan_status == ScanStatus.found)
    extra_count = sum(1 for s in scans if s.scan_status == ScanStatus.extra)
    missing_count = max(0, audit.expected_count - found_count)

    audit.status = AuditStatus.completed
    audit.completed_at = app_now()
    audit.found_count = found_count
    audit.extra_count = extra_count
    audit.missing_count = missing_count
    await db.flush()

    return RedirectResponse(
        f"/locations/audit/{audit_id}?success=Audit+closed",
        status_code=303
    )


# ─────────────────────────────────────────────────────────────────────────────
#  6. Quick location search (JSON) — for barcode scanner quick-assign
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/device-location/{barcode}")
async def api_device_location(
    barcode: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    dev_result = await db.execute(
        select(Device).where(Device.barcode == barcode)
    )
    device = dev_result.scalar_one_or_none()
    if not device:
        return JSONResponse({"error": "Device not found"}, status_code=404)

    log = await _get_device_current_location(db, str(device.id))
    loc_name = None
    if log and log.location_id:
        loc_result = await db.execute(
            select(StorageLocation).where(StorageLocation.id == log.location_id)
        )
        loc = loc_result.scalar_one_or_none()
        loc_name = loc.display_name if loc else None

    return JSONResponse({
        "barcode": device.barcode,
        "brand": device.brand,
        "model": device.model,
        "stage": device.current_stage.value,
        "current_location": loc_name,
        "last_action": log.action.value if log else None,
        "last_actor": log.actor_name if log else None,
        "last_updated": log.logged_at.isoformat() if log else None,
        "device_id": str(device.id),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  7. Gap count API (for dashboard badge refresh)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/gap-count")
async def api_gap_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    gap_ids, _, _ = await _gap_devices(db)
    return JSONResponse({"gap_count": len(gap_ids)})


# ─────────────────────────────────────────────────────────────────────────────
#  8. Zone -> Location cascade API (reused by IQC Entry, Device Edit,
#     and later the Move Device page in Batch C)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/by-zone")
async def api_locations_by_zone(
    zone: str = "",
    unit_type: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return active StorageLocation rows for a given zone (and optionally a
    Location Type / unit_type), for populating a dependent 'Location ID'
    dropdown. zone must match a ZoneType enum value (e.g. 'showroom',
    'first_floor', 'dispatch'); unit_type must match a UnitType enum value
    (e.g. 'rack', 'shelf') when provided."""
    if not zone:
        return JSONResponse([])
    try:
        zone_enum = ZoneType(zone)
    except ValueError:
        return JSONResponse([])

    conditions = [StorageLocation.zone == zone_enum, StorageLocation.is_active == True]
    if unit_type:
        try:
            conditions.append(StorageLocation.unit_type == UnitType(unit_type))
        except ValueError:
            return JSONResponse([])

    result = await db.execute(
        select(StorageLocation).where(*conditions).order_by(StorageLocation.unit_id)
    )
    locations = result.scalars().all()
    return JSONResponse([
        {"id": str(loc.id), "unit_id": loc.unit_id, "display_name": loc.display_name}
        for loc in locations
    ])
