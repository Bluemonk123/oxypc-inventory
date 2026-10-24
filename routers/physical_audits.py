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
from services.pna_lookup import active_pna_parts
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


_ANY = object()   # _devices_at_query: don't filter on this field
_RESOLVED = object()   # _devices_at_query: no location filter, return the resolved location column


def _devices_at_query(cols, stage, location_id, category, pna_group=None, only_stages=None):
    """SELECT `cols` over the devices that _count_devices_at counts: not
    trashed, current_stage == stage, resolved location == location_id,
    sub_category == category (shared so the Difference sheet lists exactly
    the tags behind Current Count)."""
    latest_q = select(DeviceLocationLog.device_id, func.max(DeviceLocationLog.logged_at).label("latest"))
    if only_stages:
        # Only the location history of tags in these stages (much less to scan).
        latest_q = latest_q.where(DeviceLocationLog.device_id.in_(
            select(Device.id).where(Device.current_stage.in_(list(only_stages)))))
    latest_sub = latest_q.group_by(DeviceLocationLog.device_id).subquery()
    latest_log = (
        select(DeviceLocationLog.device_id, DeviceLocationLog.location_id)
        .join(latest_sub, and_(DeviceLocationLog.device_id == latest_sub.c.device_id,
                               DeviceLocationLog.logged_at == latest_sub.c.latest))
        .subquery()
    )
    resolved_loc = sa_case((latest_log.c.device_id.isnot(None), latest_log.c.location_id),
                           else_=Device.location_id)
    if location_id is _RESOLVED:     # caller wants the resolved location as a column
        cols = list(cols) + [resolved_loc.label("resolved_loc")]
        location_id = _ANY
    q = (select(*cols).select_from(Device)
         .outerjoin(latest_log, latest_log.c.device_id == Device.id)
         .where(Device.is_trashed == False))  # noqa: E712
    if stage is not _ANY:
        if _stage_key(stage) == DeviceStage.scrapped:
            q = q.where(Device.current_stage.in_(SCRAP_GROUP))
        else:
            q = q.where(Device.current_stage == stage)
    if location_id is not _ANY:
        q = q.where(resolved_loc.is_(None)) if location_id is None else q.where(resolved_loc == location_id)
    if pna_group in ("PNA", "Line"):
        # L1/L2 and L3/L4 rows (2026-10-24): PNA = tags with an active PNA part.
        from models.pna_part import DevicePNAPart
        has_pna = (select(DevicePNAPart.id)
                   .where(DevicePNAPart.device_id == Device.id, DevicePNAPart.is_active == True)  # noqa: E712
                   .exists())
        q = q.where(has_pna if pna_group == "PNA" else ~has_pna)
    if category is not _ANY:
        q = q.where(Device.sub_category.is_(None)) if category is None else q.where(Device.sub_category == category)
    return q


async def _active_stock_count(db: AsyncSession) -> int:
    """Current Count of a day's audit row (2026-10-23): every tag in the
    system that should physically exist — not Sold, not in Trash."""
    return (await db.execute(
        select(func.count(Device.id))
        .where(Device.is_trashed == False, Device.current_stage != DeviceStage.sold)  # noqa: E712
    )).scalar() or 0


# Scrapped and Scrap for Sale are one audit stage (2026-10-24): picking either
# counts and matches tags in both, and the row shows "Scrapped+Scrap for Sale".
SCRAP_GROUP = (DeviceStage.scrapped, DeviceStage.scrap_for_sale)
SCRAP_GROUP_LABEL = "Scrapped+Scrap for Sale"


def _stage_key(st):
    """Audit stage for grouping / comparing: either scrap stage -> Scrapped."""
    if isinstance(st, str) and st in {g.value for g in SCRAP_GROUP}:
        return DeviceStage.scrapped
    return DeviceStage.scrapped if st in SCRAP_GROUP else st


def _stage_label(st, pna_group=None) -> str:
    """Audit stage as shown: "Scrapped+Scrap for Sale" for either scrap stage;
    "L1/L2 Repair - PNA" / "- Line" on rows split by PNA (2026-10-24)."""
    if _stage_key(st) == DeviceStage.scrapped and st is not None:
        return SCRAP_GROUP_LABEL
    base = str(STAGE_LABELS.get(st, st)) if st is not None else ""
    return f"{base} - {pna_group}" if (base and pna_group) else base


def _is_mismatch(it) -> bool:
    """A scanned tag that exists but was found at a stage or location other
    than the system's (counted in Difference)."""
    return it.device_id is not None and (_stage_key(it.my_stage) != _stage_key(it.system_stage)
                                         or it.my_location_id != it.system_location_id)


async def _difference_tags(db: AsyncSession, items, groups=None) -> tuple:
    """Difference Tags (2026-10-23): for every Audit Stage + Audit Location
    combination scanned (per Report Date row), the tags the system has there
    now (any category, not in Trash) minus every tag scanned into that row.
    Returns (rows, current_by_combo, current_ids): rows are
    (record_id, stage, location_id, barcode, lot_id, model, serial, category,
     device_id, current_stage, sub_lot_number);
    current_by_combo[(record_id, stage, location_id)] = system tags there;
    current_ids[(record_id, stage, location_id)] = their device ids."""
    combos, scanned = {}, {}
    for it in items:
        combos.setdefault((it.audit_record_id, _stage_key(it.my_stage), it.my_location_id), None)
        scanned.setdefault(it.audit_record_id, set()).add((it.barcode or "").upper())
    rows, current, current_ids = [], {}, {}
    groups = groups or {}
    if not combos:
        return rows, current, current_ids
    # PNA / Line rows (2026-10-24): a tag scanned into either row of the same
    # Report Date + Stage + Location counts as found for both, so a tag whose
    # PNA mark differs from the box ticked isn't also listed as "not scanned"
    # in the other row.
    split_rids = [rid for rid in scanned if groups.get(rid)]
    if split_rids:
        recs = (await db.execute(
            select(AuditRecord.id, AuditRecord.report_date, AuditRecord.my_stage, AuditRecord.my_location_id)
            .where(AuditRecord.id.in_(split_rids))
        )).all()
        keys = {r.id: (r.report_date, _stage_key(r.my_stage), r.my_location_id) for r in recs}
        sib = (await db.execute(
            select(AuditRecordItem.barcode, AuditRecord.report_date, AuditRecord.my_stage,
                   AuditRecord.my_location_id)
            .join(AuditRecord, AuditRecordItem.audit_record_id == AuditRecord.id)
            .where(AuditRecord.pna_group.isnot(None),
                   AuditRecord.report_date.in_({k[0] for k in keys.values()}))
        )).all()
        by_key = {}
        for b in sib:
            by_key.setdefault((b.report_date, _stage_key(b.my_stage), b.my_location_id), set()).add(
                (b.barcode or "").upper())
        for rid, k in keys.items():
            scanned[rid] |= by_key.get(k, set())
    # One query for every audited stage (2026-10-24 — per-row queries made the
    # Audit Records table slow): the tags in those stages with their resolved
    # location, then sorted into each row's stage + location (+ PNA / Line).
    stages = set()
    for _rid, st, _loc in combos:
        stages |= set(SCRAP_GROUP) if _stage_key(st) == DeviceStage.scrapped else {st}
    stages.discard(None)
    q = _devices_at_query([Device.barcode, Device.lot_id, Device.model, Device.serial_no,
                           Device.sub_category, Device.id, Device.current_stage,
                           Device.sub_lot_number], _ANY, _RESOLVED, _ANY, only_stages=stages)
    devs = (await db.execute(q.where(Device.current_stage.in_(stages)).order_by(Device.barcode))).all() \
        if stages else []
    with_pna = set()
    if any(groups.get(rid) for rid, _s, _l in combos) and devs:
        with_pna = set(await active_pna_parts(db, {d.id for d in devs}))
    live_cache = {}
    for rid, stage, loc in combos:
        grp = groups.get(rid)          # "PNA" / "Line" rows count only that half (2026-10-24)
        key = (stage, loc, grp)
        if key not in live_cache:
            live_cache[key] = [
                d for d in devs
                if _stage_key(d.current_stage) == stage and d.resolved_loc == loc
                and (grp is None or (d.id in with_pna) == (grp == "PNA"))]
        tags = live_cache[key]
        current[(rid, stage, loc)] = len(tags)
        current_ids[(rid, stage, loc)] = [t.id for t in tags]
        for t in tags:
            if (t.barcode or "").upper() not in scanned.get(rid, set()):
                rows.append((rid, stage, loc, t.barcode, t.lot_id, t.model, t.serial_no, t.sub_category,
                             t.id, t.current_stage, t.sub_lot_number))
    return rows, current, current_ids


PNA_STAGES = {DeviceStage.l1, DeviceStage.l2, DeviceStage.l3}   # L1/L2 and L3/L4 rows show PNA counts


async def _record_stats(db: AsyncSession, record_ids, pna=None) -> dict:
    """{record_id: (physical, difference, missing)}: physical = scanned tags
    that exist; difference = Difference Tags (expected at the scanned
    stage/location combinations but not scanned); missing = scanned Tag
    Numbers not in the system.

    pna (a dict, optional): filled with {record_id: {"current", "physical",
    "difference"}} PNA tag counts (tags with an active PNA part) for rows
    audited at L1/L2 or L3/L4 Repair (2026-10-24)."""
    out = {}
    items = []
    if record_ids:
        items = (await db.execute(
            select(AuditRecordItem).where(AuditRecordItem.audit_record_id.in_(list(record_ids)))
        )).scalars().all()
    for it in items:
        p_, d_, m_ = out.get(it.audit_record_id, (0, 0, 0))
        if it.device_id is None:
            m_ += 1
        else:
            p_ += 1
        out[it.audit_record_id] = (p_, d_, m_)
    groups = {}
    if record_ids:
        groups = dict((await db.execute(
            select(AuditRecord.id, AuditRecord.pna_group).where(AuditRecord.id.in_(list(record_ids)))
        )).all())
    diff_rows, _cur, cur_ids = await _difference_tags(db, items, groups)
    for row in diff_rows:
        p_, d_, m_ = out.get(row[0], (0, 0, 0))
        out[row[0]] = (p_, d_ + 1, m_)
    if pna is not None:
        pna_rids = {it.audit_record_id for it in items if it.my_stage in PNA_STAGES}
        if pna_rids:
            ids = {it.device_id for it in items if it.audit_record_id in pna_rids and it.device_id}
            ids |= {d for (rid, _s, _l), v in cur_ids.items() if rid in pna_rids for d in v}
            with_pna = set(await active_pna_parts(db, ids)) if ids else set()
            for rid in pna_rids:
                pna[rid] = {
                    "current": len({d for (r, st, _l), v in cur_ids.items() if r == rid and st in PNA_STAGES
                                    for d in v if d in with_pna}),
                    "physical": len({it.device_id for it in items if it.audit_record_id == rid
                                     and it.my_stage in PNA_STAGES and it.device_id in with_pna}),
                    "difference": sum(1 for r in diff_rows if r[0] == rid and r[1] in PNA_STAGES
                                      and r[8] in with_pna),
                }
    return out


async def _count_devices_at(db: AsyncSession, stage, location_id, category, pna_group=None) -> int:
    """Live count of Devices (is_trashed=False) whose current_stage == stage,
    whose resolved system location == location_id, and whose sub_category ==
    category — using the same latest-log precedence as
    _resolve_device_system_location_id but batched into one query instead of
    one-per-device. This is "how many does the system say should be here",
    compared against physical_count ("how many were actually found")."""
    q = _devices_at_query([func.count(Device.id)], stage, location_id, category, pna_group)
    return (await db.execute(q)).scalar() or 0


async def _already_audited(db: AsyncSession, barcode: str, r_date) -> bool:
    """True if this exact tag already has an AuditRecordItem for this report
    date — the "no duplicate tags for the same date" rule. Checked at both
    Lookup (immediate feedback before the auditor picks anything) and Submit
    (authoritative gate)."""
    exists = (await db.execute(
        select(AuditRecordItem.id)
        .join(AuditRecord, AuditRecordItem.audit_record_id == AuditRecord.id)
        .where(AuditRecord.report_date == r_date, func.upper(AuditRecordItem.barcode) == (barcode or "").upper())
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
    r_date = None
    try:
        r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        pass
    if r_date and await _already_audited(db, device.barcode if device else bc, r_date):
        return JSONResponse({"found": False, "error": "You already audited this tag."})
    if not device:
        # Not in the system: the page still submits it, recorded as Missing.
        return JSONResponse({"found": False, "unknown": True, "barcode": bc})

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
        # Receipt's PNA Matched / Mismatch for L1/L2 and L3/L4 (2026-10-24).
        "pna": bool(await active_pna_parts(db, [device.id])),
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
    pna: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record one scanned tag (rules of 2026-10-24):
      * one Audit Records row per Report Date + Audit/Physical Stage +
        Audit/Physical Location (Category is kept per tag, in the sheets,
        not as a row key);
      * Current Count = tags the system has at that stage + location (any
        category, not in Trash) when the row's first tag is scanned;
      * a tag that exists adds to Physical Count; one whose stage or location
        differs from the system's counts in Difference (worked out on read);
      * a Tag Number that isn't in the system is recorded as Missing (only)."""
    try:
        r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return JSONResponse({"ok": False, "error": "Invalid report date."})
    bc = (barcode or "").strip()
    if not bc:
        return JSONResponse({"ok": False, "error": "Enter a Tag Number."})
    device = (await db.execute(select(Device).where(Device.barcode.ilike(bc)))).scalars().first()
    if await _already_audited(db, device.barcode if device else bc, r_date):
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

    # The row for this Report Date + Stage + Location (2026-10-24: a new row
    # whenever any of the three differs). A row saved before 2026-10-23 also
    # carries a Category — prefer the one without, else join the oldest.
    loc_match = (AuditRecord.my_location_id == my_loc_uuid) if my_loc_uuid else AuditRecord.my_location_id.is_(None)
    # L1/L2 and L3/L4 (2026-10-24): the PNA checkbox gives its own row,
    # "<stage> - PNA"; unticked is "<stage> - Line".
    pna_group = (("PNA" if (pna or "").strip().lower() in ("1", "on", "true", "yes") else "Line")
                 if my_stage_enum in PNA_STAGES else None)
    grp_match = (AuditRecord.pna_group == pna_group) if pna_group else AuditRecord.pna_group.is_(None)
    audit_record = (await db.execute(
        select(AuditRecord).where(AuditRecord.report_date == r_date,
                                  (AuditRecord.my_stage.in_(SCRAP_GROUP) if my_stage_enum in SCRAP_GROUP
                                   else AuditRecord.my_stage == my_stage_enum),
                                  loc_match, grp_match)
        .order_by(AuditRecord.category.is_(None).desc(), AuditRecord.created_at.asc())
    )).scalars().first()
    if not audit_record:
        audit_record = AuditRecord(report_date=r_date, my_stage=my_stage_enum, my_location_id=my_loc_uuid,
                                   pna_group=pna_group,
                                   current_count=await _count_devices_at(db, my_stage_enum, my_loc_uuid, _ANY,
                                                                         pna_group),
                                   physical_count=0)
        db.add(audit_record)
        await db.flush()

    if device is None:
        db.add(AuditRecordItem(
            audit_record_id=audit_record.id, device_id=None, barcode=bc[:100],
            system_stage=None, system_location_id=None,
            my_stage=my_stage_enum, my_location_id=my_loc_uuid,
            audited_by_user_id=current_user.id,
        ))
        audit_record.updated_at = app_now()
        await db.commit()
        return JSONResponse({"ok": True, "missing": True, "audit_record_id": str(audit_record.id)})

    system_location_id = await _resolve_device_system_location_id(db, device)
    lot_number = None
    if device.lot_id:
        lot_number = (await db.execute(select(Lot.lot_number).where(Lot.id == device.lot_id))).scalar()
    audit_record.physical_count = (audit_record.physical_count or 0) + 1
    audit_record.updated_at = app_now()
    db.add(AuditRecordItem(
        audit_record_id=audit_record.id, device_id=device.id, barcode=device.barcode,
        lot_number=lot_number, brand=device.brand, model=device.model, serial_no=device.serial_no,
        category=device.sub_category, system_stage=device.current_stage,
        system_location_id=system_location_id, my_stage=my_stage_enum, my_location_id=my_loc_uuid,
        assigned_by_user_id=None,   # see module docstring
        assigned_to_user_id=device.assigned_to_user_id, audited_by_user_id=current_user.id,
    ))
    await db.commit()
    return JSONResponse({"ok": True, "missing": False, "audit_record_id": str(audit_record.id),
                         "current_count": audit_record.current_count,
                         "physical_count": audit_record.physical_count})


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

    pna_counts = {}
    stats = await _record_stats(db, [r.id for r in records], pna=pna_counts)
    # Audit Stage / Audit Location columns: every stage and location scanned
    # into the row (a day's row can cover several), in first-scanned order.
    stages_by, locs_by = {}, {}
    if records:
        for rid, st, lc in (await db.execute(
            select(AuditRecordItem.audit_record_id, AuditRecordItem.my_stage, AuditRecordItem.my_location_id)
            .where(AuditRecordItem.audit_record_id.in_([r.id for r in records]))
            .order_by(AuditRecordItem.created_at.asc())
        )).all():
            if st is not None and st not in stages_by.setdefault(rid, []):
                stages_by[rid].append(st)
            if lc is not None and lc not in locs_by.setdefault(rid, []):
                locs_by[rid].append(lc)
    extra_locs = {lc for v in locs_by.values() for lc in v} - set(loc_map)
    if extra_locs:
        for loc in (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(extra_locs)))).scalars().all():
            loc_map[loc.id] = loc
    data = []
    for r in records:
        physical, difference, missing = stats.get(r.id, (0, 0, 0))
        # Rows saved before 2026-10-23 carry their own single stage/location.
        st_list = stages_by.get(r.id) or ([r.my_stage] if r.my_stage else [])
        lc_list = locs_by.get(r.id) or ([r.my_location_id] if r.my_location_id else [])
        data.append({
            "id": str(r.id),
            "report_date": r.report_date.isoformat() if r.report_date else "",
            "my_stage": ", ".join(dict.fromkeys(_stage_label(x, r.pna_group) for x in st_list)) or "—",
            "my_location": ", ".join(_loc_short(loc_map.get(x)) for x in lc_list) or "—",
            "current_count": r.current_count,
            "physical_count": physical,
            "difference": difference,
            "missing": missing,
            "notes": r.notes or "",
            # L1/L2 and L3/L4 rows only: PNA tags within each count (2026-10-24).
            "pna": pna_counts.get(r.id),
            "pna_group": r.pna_group or "",
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

# ── Excel workbook: Tags Summary + Stage Summary + Location Summary ─────────
# (2026-10-23) One workbook for both the per-record Download and the card
# header's Export: the three sheets always come from the same set of audit
# records, so their totals agree.

async def _audit_workbook(db: AsyncSession, records: list) -> bytes:
    """Five sheets (2026-10-23):
      Physical Tags    - scanned tags that exist in the system (with Category,
                         system vs audit stage/location, Matched/Unmatched)
      Difference Tags  - tags the system has at each scanned Audit Stage +
                         Audit Location, not scanned (_difference_tags)
      Missing Tags     - scanned Tag Numbers not in the system
      Stage Summary    - per Audit Stage: system count there now, physical,
                         difference, missing
      Location Summary - the same per Audit Location"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    rec_by_id = {r.id: r for r in records}
    items = []
    if records:
        items = (await db.execute(
            select(AuditRecordItem)
            .where(AuditRecordItem.audit_record_id.in_(list(rec_by_id)))
            .order_by(AuditRecordItem.created_at.asc())
        )).scalars().all()
    loc_ids = {it.system_location_id for it in items if it.system_location_id}
    loc_ids |= {it.my_location_id for it in items if it.my_location_id}
    loc_map = {}
    if loc_ids:
        locs = (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(loc_ids)))).scalars().all()
        loc_map = {loc.id: loc for loc in locs}
    users = {}
    uids = {it.audited_by_user_id for it in items if it.audited_by_user_id}
    if uids:
        users = {u.id: (u.full_name or u.username) for u in
                 (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()}

    def _date(d):
        return d.strftime("%d-%m-%Y") if d else ""

    items = sorted(items, key=lambda i: (rec_by_id[i.audit_record_id].report_date or date.min,
                                         i.created_at or datetime.min))
    found = [it for it in items if it.device_id is not None]
    unknown = [it for it in items if it.device_id is None]

    # PNA + Stock Type for the Physical Tags sheet (2026-10-24): the tag's
    # active PNA parts and stock type now.
    from routers.devices import STOCK_TYPE_LABELS, stock_type_key
    found_ids = {it.device_id for it in found}
    found_pna = set(await active_pna_parts(db, found_ids)) if found_ids else set()
    found_dev = {}
    if found_ids:
        found_dev = {i: (st, sub) for i, st, sub in (await db.execute(
            select(Device.id, Device.current_stage, Device.sub_lot_number).where(Device.id.in_(found_ids))
        )).all()}

    def _tag_row(it):
        rec = rec_by_id[it.audit_record_id]
        st, sub = found_dev.get(it.device_id, (it.system_stage, None))
        return [_date(rec.report_date), it.barcode, it.lot_number or "", it.model or "", it.serial_no or "",
                it.category or "",
                "Yes" if it.device_id in found_pna else "No", STOCK_TYPE_LABELS[stock_type_key(st, sub)],
                STAGE_LABELS.get(it.system_stage, it.system_stage), _loc_short(loc_map.get(it.system_location_id)),
                _stage_label(it.my_stage, rec.pna_group), _loc_short(loc_map.get(it.my_location_id)),
                _match_text(it, rec.pna_group, it.device_id in found_pna), it.notes or ""]

    def _match_text(it, grp, has_pna):
        # PNA / Line rows (2026-10-24): the system's PNA mark must agree too.
        if _is_mismatch(it):
            return "Unmatched"
        if grp and (grp == "PNA") != has_pna:
            return "Unmatched (PNA)"
        return "Matched"

    phys = [_tag_row(it) for it in found]
    diff_raw, current_by_combo, _cur_ids = await _difference_tags(
        db, items, {rid: r.pna_group for rid, r in rec_by_id.items()})
    # PNA + Stock Type for the Difference Tags sheet (2026-10-24).
    diff_pna = set(await active_pna_parts(db, {r[8] for r in diff_raw})) if diff_raw else set()
    lot_ids = {r[4] for r in diff_raw if r[4]}
    lot_no = {}
    if lot_ids:
        lot_no = dict((await db.execute(select(Lot.id, Lot.lot_number).where(Lot.id.in_(lot_ids)))).all())
    loc_ids2 = {r[2] for r in diff_raw if r[2]} - set(loc_map)
    if loc_ids2:
        for loc in (await db.execute(select(StorageLocation).where(StorageLocation.id.in_(loc_ids2)))).scalars().all():
            loc_map[loc.id] = loc
    diff = [[_date(rec_by_id[r[0]].report_date), r[3], lot_no.get(r[4], "") if r[4] else "", r[5] or "",
             r[6] or "", r[7] or "",
             "Yes" if r[8] in diff_pna else "No", STOCK_TYPE_LABELS[stock_type_key(r[9], r[10])],
             STAGE_LABELS.get(r[9], r[9]), _loc_short(loc_map.get(r[2])),
             _stage_label(r[1], rec_by_id[r[0]].pna_group), _loc_short(loc_map.get(r[2])),
             "Not scanned"] for r in diff_raw]
    # Missing Tags are not in the system, so PNA / Stock Type / Current Stage /
    # Current Location have nothing to show.
    missing = [[_date(rec_by_id[it.audit_record_id].report_date), it.barcode, "—",
                "—", "—", "—", "—",
                _stage_label(it.my_stage, rec_by_id[it.audit_record_id].pna_group),
                _loc_short(loc_map.get(it.my_location_id)),
                users.get(it.audited_by_user_id, ""),
                it.created_at.strftime("%d-%m-%Y %H:%M") if it.created_at else ""] for it in unknown]

    def _summary(key_of_combo, key_of_item, label_fn):
        g = {}
        for (rid, stage, loc), n in current_by_combo.items():
            g.setdefault(key_of_combo(rid, stage, loc), [0, 0, 0, 0])[0] += n
        for it in items:
            k = key_of_item(it)
            row = g.setdefault(k, [0, 0, 0, 0])
            if it.device_id is None:
                row[3] += 1
            else:
                row[1] += 1
        for r in diff_raw:
            g.setdefault(key_of_combo(r[0], r[1], r[2]), [0, 0, 0, 0])[2] += 1
        return [[label_fn(k)] + v for k, v in g.items()]

    _grp = lambda rid: rec_by_id[rid].pna_group
    stage_rows = _summary(lambda rid, st, lc: (st, _grp(rid)),
                          lambda it: (_stage_key(it.my_stage), _grp(it.audit_record_id)),
                          lambda k: _stage_label(k[0], k[1]))
    loc_rows = _summary(lambda rid, st, lc: lc, lambda it: it.my_location_id, lambda k: _loc_short(loc_map.get(k)))

    phys_header = ["Report Date", "Tag Number", "Lot Number", "Model", "Serial", "Category", "PNA", "Stock Type",
                   "Current Stage", "Current Location", "Audit/Physical Stage", "Audit/Physical Location",
                   "Stage/Location Match", "Notes"]
    sum_cols = ["Current Count (system now)", "Physical Count", "Difference", "Missing"]
    sheets = [
        ("Physical Tags", phys_header, phys),
        ("Difference Tags", ["Report Date", "Tag Number", "Lot Number", "Model", "Serial", "Category",
                             "PNA", "Stock Type", "Current Stage", "Current Location",
                             "Audit/Physical Stage", "Audit/Physical Location", "Status"], diff),
        ("Missing Tags", ["Report Date", "Tag Number", "Category",
                          "PNA", "Stock Type", "Current Stage", "Current Location",
                          "Audit/Physical Stage", "Audit/Physical Location",
                          "Audited By", "Scanned At"], missing),
        ("Stage Summary", ["Audit/Physical Stage"] + sum_cols, stage_rows),
        ("Location Summary", ["Audit/Physical Location"] + sum_cols, loc_rows),
    ]
    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="0377C2")
    for i, (title, header, rows) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 40)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_response(data: bytes, filename: str) -> Response:
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/records/{record_id}/download")
async def download_record(
    record_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One audit record as a workbook: its tags, plus its Stage and Location
    summary rows."""
    record = (await db.execute(select(AuditRecord).where(AuditRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Audit record not found."}, status_code=404)
    data = await _audit_workbook(db, [record])
    # File name = Report Date + Audit/Physical Stage column value (2026-10-24),
    # e.g. "24_10_2026_L1-L2 Repair - PNA.xlsx" ("/" is not allowed in names).
    stage_txt = (_stage_label(record.my_stage, record.pna_group) or "Audit").replace("/", "-")
    return _xlsx_response(data, f"{record.report_date.strftime('%d_%m_%Y')}_{stage_txt}.xlsx")


@router.get("/export/workbook")
async def export_workbook(
    report_date: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Card header Export: every audit record (or one Report Date's) as a
    workbook with Tags Summary, Stage Summary and Location Summary sheets."""
    q = select(AuditRecord).order_by(AuditRecord.report_date.desc(), AuditRecord.created_at.desc())
    # File name DD_MM_YYYY_Physical_Audit_Report (2026-10-24): the filtered
    # Report Date, or today's date when exporting every record.
    name_date = date.today()
    if report_date.strip():
        try:
            r_date = datetime.strptime(report_date.strip(), "%Y-%m-%d").date()
            q = q.where(AuditRecord.report_date == r_date)
            name_date = r_date
        except ValueError:
            pass
    records = (await db.execute(q)).scalars().all()
    data = await _audit_workbook(db, records)
    return _xlsx_response(data, f"{name_date.strftime('%d_%m_%Y')}_Physical_Audit_Report.xlsx")


# ── Whole-table exports ──────────────────────────────────────────────────────
