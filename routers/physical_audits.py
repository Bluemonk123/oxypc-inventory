"""
Physical Audits — Report Date x Audit Stage x Audit Location x Category tag audits.

Deliberately a brand-new feature, separate from the older "Physical Audit"
zone/scan-batch tool (InventoryAudit/AuditScanItem in models/location.py,
mounted under Inventory Locations -> /locations/audit, relabelled "Zone
Audit" in the sidebar so the two don't read as duplicates). Do not touch
that feature's code from here.

"Assigned By" sourcing note (see AuditRecordItem.assigned_by_user_id):
Device.assigned_to_user_id (WHO a tag is currently assigned to) is written
from several different flows (routers/grn.py Post-IQC, routers/transfers.py
Move Bucket/Move Lot/Move Item) and none of them record a matching "assigned
by" user id anywhere on Device — the closest candidates are
StockTransfer.transferred_by (a free-text username string, not a user_id FK)
and DeviceLocationLog.actor_id (who performed a *location* move, not a tag
*assignment*). Neither is a reliable "who assigned this tag to its current
holder" source, so assigned_by_user_id is always left NULL here and the UI
renders "-" for it, per this feature's spec.
"""
import csv
import io
import uuid as uuid_module
from datetime import datetime, date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy import select, func, and_, case as sa_case
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from templates_config import templates
from utils.timezone import app_now
from auth.dependencies import get_current_user, verify_csrf
from models.user import User
from models.device import Device, DeviceStage, STAGE_LABELS, DROPDOWN_STAGES
from models.location import StorageLocation, DeviceLocationLog
from models.lot import Lot
from models.audit_record import AuditRecord, AuditRecordItem
# Reused rather than reimplemented — same "latest DeviceLocationLog row for
# this device" resolution every other location-aware page in the app uses.
from routers.devices import _build_location_map

router = APIRouter(prefix="/audits", tags=["physical-audits"], dependencies=[Depends(verify_csrf)])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _active_location_options(locations):
    return [{"id": str(loc.id), "label": _loc_short(loc)} for loc in locations]


def _loc_short(loc) -> str:
    """Shorter location label for the Perform Audit form/brief info only —
    just the unit id (+ slot), dropping the "Zone -> Unit Type" prefix that
    StorageLocation.display_name adds. The Audit Records table and exports
    keep the full display_name/_loc_display for record-keeping clarity."""
    if not loc:
        return "—"
    return f"{loc.unit_id} [{loc.slot}]" if loc.slot else loc.unit_id


def _stage_options():
    return [(s.value, STAGE_LABELS.get(s, s.value)) for s in DROPDOWN_STAGES]


async def _resolve_device_system_location_id(db: AsyncSession, device: Device):
    """Same precedence used across the app: the most-recent DeviceLocationLog
    row's location_id (which may itself be None — device currently "in
    hand") if a log exists at all, else Device.location_id."""
    info = (await _build_location_map(db, [str(device.id)])).get(str(device.id))
    if info is not None:
        return info.get("location_id")
    return device.location_id


async def _count_devices_at(db: AsyncSession, stage, location_id, category) -> int:
    """Live count of Devices (is_trashed=False) whose current_stage == stage,
    whose resolved system location == location_id, and whose sub_category ==
    category — using the same latest-log precedence as
    _resolve_device_system_location_id but batched into one query instead of
    one-per-device. This is "how many does the system say should be here",
    compared against physical_count ("how many were actually found")."""
    latest_sub = (
        select(
            DeviceLocationLog.device_id,
            func.max(DeviceLocationLog.logged_at).label("latest"),
        )
        .group_by(DeviceLocationLog.device_id)
        .subquery()
    )
    latest_log = (
        select(DeviceLocationLog.device_id, DeviceLocationLog.location_id)
        .join(latest_sub, and_(
            DeviceLocationLog.device_id == latest_sub.c.device_id,
            DeviceLocationLog.logged_at == latest_sub.c.latest,
        ))
        .subquery()
    )
    resolved_loc = sa_case(
        (latest_log.c.device_id.isnot(None), latest_log.c.location_id),
        else_=Device.location_id,
    )
    q = (
        select(func.count(Device.id))
        .select_from(Device)
        .outerjoin(latest_log, latest_log.c.device_id == Device.id)
        .where(Device.current_stage == stage, Device.is_trashed == False)
    )
    if location_id is None:
        q = q.where(resolved_loc.is_(None))
    else:
        q = q.where(resolved_loc == location_id)
    if category is None:
        q = q.where(Device.sub_category.is_(None))
    else:
        q = q.where(Device.sub_category == category)
    return (await db.execute(q)).scalar() or 0


async def _already_audited(db: AsyncSession, barcode: str, r_date) -> bool:
    """True if this exact tag already has an AuditRecordItem for this report
    date — the "no duplicate tags for the same date" rule. Checked at both
    Lookup (immediate feedback before the auditor picks anything) and Submit
    (authoritative gate)."""
    exists = (await db.execute(
        select(AuditRecordItem.id)
        .join(AuditRecord, AuditRecordItem.audit_record_id == AuditRecord.id)
        .where(AuditRecord.report_date == r_date, AuditRecordItem.barcode == barcode)
        .limit(1)
    )).scalar_one_or_none()
    return exists is not None


def _csv_response(header: list, rows: list, filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    output.seek(0)
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _loc_display(loc: StorageLocation) -> str:
    return loc.display_name if loc else "—"


# ── Main page ────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def audits_home(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    locations = (await db.execute(
        select(StorageLocation)
        .where(StorageLocation.is_active == True)
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )).scalars().all()

    return templates.TemplateResponse("audits/list.html", {
        "request": request,
        "current_user": current_user,
        "today": date.today().isoformat(),
        "stage_options": _stage_options(),
        "location_options": _active_location_options(locations),
    })


# ── Tag lookup (Perform Audit search box) ──────────────────────────────────

@router.get("/lookup")
async def lookup_device(
    barcode: str = "",
    report_date: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    bc = (barcode or "").strip()
    if not bc:
        return JSONResponse({"found": False})
    device = (await db.execute(
        select(Device).where(Device.barcode.ilike(bc))
    )).scalars().first()
    if not device:
        return JSONResponse({"found": False, "error": f"No device found for tag {bc}."})

    r_date = None
    try:
        r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        pass
    if r_date and await _already_audited(db, device.barcode, r_date):
        return JSONResponse({"found": False, "error": "You already audited this tag."})

    lot_number = None
    if device.lot_id:
        lot_number = (await db.execute(
            select(Lot.lot_number).where(Lot.id == device.lot_id)
        )).scalar()

    system_location_id = await _resolve_device_system_location_id(db, device)
    system_location = None
    if system_location_id:
        system_location = (await db.execute(
            select(StorageLocation).where(StorageLocation.id == system_location_id)
        )).scalar_one_or_none()

    assigned_to_name = None
    if device.assigned_to_user_id:
        assigned_user = (await db.execute(
            select(User).where(User.id == device.assigned_to_user_id)
        )).scalar_one_or_none()
        assigned_to_name = assigned_user.full_name if assigned_user else None

    return JSONResponse({
        "found": True,
        "barcode": device.barcode,
        "brand": device.brand or "—",
        "model": device.model or "—",
        "serial_no": device.serial_no or "—",
        "lot_number": lot_number or "—",
        "system_stage": device.current_stage.value if device.current_stage else None,
        "system_stage_label": STAGE_LABELS.get(device.current_stage, device.current_stage),
        "system_location_id": str(system_location_id) if system_location_id else "",
        "system_location_label": _loc_short(system_location),
        "category": device.sub_category or "—",
        # "Assigned By" is intentionally not returned — see module docstring.
        "assigned_by_label": "—",
        "assigned_to_label": assigned_to_name or "—",
    })


# ── Submit (Done button) ────────────────────────────────────────────────────

@router.post("/submit")
async def submit_audit(
    report_date: str = Form(...),
    barcode: str = Form(...),
    my_stage: str = Form(...),
    my_location_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return JSONResponse({"ok": False, "error": "Invalid report date."})

    bc = (barcode or "").strip()
    device = (await db.execute(select(Device).where(Device.barcode.ilike(bc)))).scalars().first()
    if not device:
        return JSONResponse({"ok": False, "error": f"No device found for tag {bc}."})

    if await _already_audited(db, device.barcode, r_date):
        return JSONResponse({"ok": False, "error": "You already audited this tag."})

    try:
        my_stage_enum = DeviceStage(my_stage)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid stage selected."})

    my_loc_uuid = None
    if (my_location_id or "").strip():
        try:
            my_loc_uuid = uuid_module.UUID(my_location_id.strip())
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid location selected."})

    system_stage = device.current_stage
    system_location_id = await _resolve_device_system_location_id(db, device)
    category = device.sub_category

    lot_number = None
    if device.lot_id:
        lot_number = (await db.execute(
            select(Lot.lot_number).where(Lot.id == device.lot_id)
        )).scalar()

    # ── Find or create the parent AuditRecord for (report_date, my_stage, my_location_id, category) ──
    parent_q = select(AuditRecord).where(
        AuditRecord.report_date == r_date,
        AuditRecord.my_stage == my_stage_enum,
    )
    parent_q = parent_q.where(
        AuditRecord.my_location_id.is_(None) if my_loc_uuid is None
        else AuditRecord.my_location_id == my_loc_uuid
    )
    parent_q = parent_q.where(
        AuditRecord.category.is_(None) if category is None
        else AuditRecord.category == category
    )
    audit_record = (await db.execute(parent_q)).scalars().first()

    if audit_record:
        audit_record.physical_count = (audit_record.physical_count or 0) + 1
        audit_record.updated_at = app_now()
    else:
        current_count = await _count_devices_at(db, my_stage_enum, my_loc_uuid, category)
        audit_record = AuditRecord(
            report_date=r_date,
            my_stage=my_stage_enum,
            my_location_id=my_loc_uuid,
            category=category,
            current_count=current_count,
            physical_count=1,
        )
        db.add(audit_record)
        await db.flush()

    item = AuditRecordItem(
        audit_record_id=audit_record.id,
        device_id=device.id,
        barcode=device.barcode,
        lot_number=lot_number,
        brand=device.brand,
        model=device.model,
        serial_no=device.serial_no,
        category=category,
        system_stage=system_stage,
        system_location_id=system_location_id,
        my_stage=my_stage_enum,
        my_location_id=my_loc_uuid,
        assigned_by_user_id=None,   # see module docstring
        assigned_to_user_id=device.assigned_to_user_id,
        audited_by_user_id=current_user.id,
    )
    db.add(item)
    await db.commit()

    return JSONResponse({
        "ok": True,
        "audit_record_id": str(audit_record.id),
        "current_count": audit_record.current_count,
        "physical_count": audit_record.physical_count,
        "difference": audit_record.difference,
    })


# ── Audit Records table ──────────────────────────────────────────────────────

@router.get("/records")
async def list_records(
    report_date: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(AuditRecord).order_by(AuditRecord.report_date.desc(), AuditRecord.created_at.desc())
    if report_date.strip():
        try:
            r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
            q = q.where(AuditRecord.report_date == r_date)
        except ValueError:
            pass
    records = (await db.execute(q)).scalars().all()

    loc_ids = {r.my_location_id for r in records if r.my_location_id}
    loc_map = {}
    if loc_ids:
        locs = (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(loc_ids)))).scalars().all()
        loc_map = {loc.id: loc for loc in locs}

    data = []
    for r in records:
        data.append({
            "id": str(r.id),
            "report_date": r.report_date.isoformat() if r.report_date else "",
            "my_stage": STAGE_LABELS.get(r.my_stage, r.my_stage),
            "my_location": _loc_short(loc_map.get(r.my_location_id)),
            "category": r.category or "—",
            "current_count": r.current_count,
            "physical_count": r.physical_count,
            "difference": r.difference,
            "notes": r.notes or "",
        })
    return JSONResponse({"data": data})


@router.post("/records/{record_id}/notes")
async def update_record_notes(
    record_id: str,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = (await db.execute(select(AuditRecord).where(AuditRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Audit record not found."})
    record.notes = (notes or "").strip() or None
    record.updated_at = app_now()
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/records/{record_id}/delete")
async def delete_record(
    record_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = (await db.execute(select(AuditRecord).where(AuditRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Audit record not found."})
    items = (await db.execute(
        select(AuditRecordItem).where(AuditRecordItem.audit_record_id == record.id)
    )).scalars().all()
    for it in items:
        await db.delete(it)
    await db.delete(record)
    await db.commit()
    return JSONResponse({"ok": True})


# ── Per-row download (one AuditRecord's tag items) ──────────────────────────

@router.get("/records/{record_id}/download")
async def download_record(
    record_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = (await db.execute(select(AuditRecord).where(AuditRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Audit record not found."}, status_code=404)

    items = (await db.execute(
        select(AuditRecordItem)
        .where(AuditRecordItem.audit_record_id == record.id)
        .order_by(AuditRecordItem.created_at.asc())
    )).scalars().all()

    loc_ids = {it.system_location_id for it in items if it.system_location_id}
    loc_ids |= {it.my_location_id for it in items if it.my_location_id}
    loc_map = {}
    if loc_ids:
        locs = (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(loc_ids)))).scalars().all()
        loc_map = {loc.id: loc for loc in locs}

    header = [
        "Tag Number", "Lot Number", "Model", "Serial", "Category", "Current Stage", "Current Location",
        "Audit Stage", "Audit Location", "Difference", "Notes",
    ]
    rows = []
    for it in items:
        stage_match = it.my_stage == it.system_stage
        loc_match = it.my_location_id == it.system_location_id
        # "Difference" = Yes when either the stage or the location the
        # auditor entered doesn't match what the system had on record for
        # this tag at audit time; No when both matched. (Decision: the spec
        # left this column's exact meaning to judgement — a plain "was there
        # a difference" flag is more directly useful on a per-item export
        # than leaving it blank.)
        difference = "No" if (stage_match and loc_match) else "Yes"
        rows.append([
            it.barcode, it.lot_number or "", it.model or "", it.serial_no or "", it.category or "",
            STAGE_LABELS.get(it.system_stage, it.system_stage),
            _loc_short(loc_map.get(it.system_location_id)),
            STAGE_LABELS.get(it.my_stage, it.my_stage),
            _loc_short(loc_map.get(it.my_location_id)),
            difference,
            it.notes or "",
        ])
    fname = f"audit_record_{record.report_date}_{record.id}.csv"
    return _csv_response(header, rows, fname)


# ── Whole-table exports ──────────────────────────────────────────────────────

@router.get("/export/stage-summary")
async def export_stage_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    records = (await db.execute(select(AuditRecord))).scalars().all()
    totals = {}
    for r in records:
        key = r.my_stage
        t = totals.setdefault(key, {"current": 0, "physical": 0})
        t["current"] += r.current_count or 0
        t["physical"] += r.physical_count or 0
    header = ["Audit Stage", "Current Count", "Audit Count", "Difference"]
    rows = [
        [STAGE_LABELS.get(stage, stage), t["current"], t["physical"], t["current"] - t["physical"]]
        for stage, t in totals.items()
    ]
    return _csv_response(header, rows, "audit_stage_summary.csv")


@router.get("/export/location-summary")
async def export_location_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    records = (await db.execute(select(AuditRecord))).scalars().all()
    loc_ids = {r.my_location_id for r in records if r.my_location_id}
    loc_map = {}
    if loc_ids:
        locs = (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(loc_ids)))).scalars().all()
        loc_map = {loc.id: loc for loc in locs}

    totals = {}
    for r in records:
        key = r.my_location_id
        t = totals.setdefault(key, {"current": 0, "physical": 0})
        t["current"] += r.current_count or 0
        t["physical"] += r.physical_count or 0
    header = ["Audit Location", "Current Count", "Audit Count", "Difference"]
    rows = [
        [_loc_short(loc_map.get(loc_id)), t["current"], t["physical"], t["current"] - t["physical"]]
        for loc_id, t in totals.items()
    ]
    return _csv_response(header, rows, "audit_location_summary.csv")


@router.get("/export/tags-summary")
async def export_tags_summary(
    report_date: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        select(AuditRecordItem, AuditRecord.report_date)
        .join(AuditRecord, AuditRecordItem.audit_record_id == AuditRecord.id)
        .order_by(AuditRecord.report_date.desc(), AuditRecordItem.created_at.asc())
    )
    if report_date.strip():
        try:
            r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
            q = q.where(AuditRecord.report_date == r_date)
        except ValueError:
            pass
    rows_raw = (await db.execute(q)).all()

    loc_ids = {it.system_location_id for it, _ in rows_raw if it.system_location_id}
    loc_ids |= {it.my_location_id for it, _ in rows_raw if it.my_location_id}
    loc_map = {}
    if loc_ids:
        locs = (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(loc_ids)))).scalars().all()
        loc_map = {loc.id: loc for loc in locs}

    header = [
        "Report Date", "Tag Number", "Lot Number", "Model", "Serial", "Category",
        "Current Stage", "Current Location", "Audit Stage", "Audit Location", "Notes",
    ]
    rows = []
    for it, rdate in rows_raw:
        rows.append([
            rdate.isoformat() if rdate else "",
            it.barcode, it.lot_number or "", it.model or "", it.serial_no or "", it.category or "",
            STAGE_LABELS.get(it.system_stage, it.system_stage),
            _loc_short(loc_map.get(it.system_location_id)),
            STAGE_LABELS.get(it.my_stage, it.my_stage),
            _loc_short(loc_map.get(it.my_location_id)),
            it.notes or "",
        ])
    return _csv_response(header, rows, "audit_tags_summary.csv")
