"""
Buckets Router — Carton/bucket grouping for Stock Inward → TRC Production flow
"""
import uuid
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, reset_repair_cycle_on_entry
from models.bucket import Bucket, _new_bucket_number
from models.master import MasterData
from models.stock_transfer import StockTransfer
from models.work_order import WorkOrder
from models.location import StorageLocation
from auth.dependencies import get_current_user, require_roles, verify_csrf
from services.notifications import create_notification
from services.location_defaults import ensure_stage_location
from services.audit_engine import audit
from services.work_order_close import close_open_work_orders, reuse_open_stress_work_order

router = APIRouter(tags=["buckets"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager)

FALLBACK_WAREHOUSES = [
    "TRC 1st Floor", "TRC 2nd Floor", "TRC 3rd Floor",
    "Bluemonk House Showroom", "Bluemonk Showroom", "Other",
]

# L1 and L2 engineers share the merged /repair/l1 queue and the l2 stage is retired, so an
# L2 assignment lands in l1 — routing it to DeviceStage.l2 would strand the whole bucket.
DEPT_TO_STAGE = {
    "L1 Engineer": "l1", "L2 Engineer": "l1",
    # Assign Bucket modal's 3 radio options (Production Manager). "Stress
    # Test" is the department VALUE (unchanged, still keys DEPT_TO_ROLE and
    # ASG_LEVEL_ROLE in trc_production.html) even though its radio LABEL now
    # reads "QC or Stress" — it targets the qc_check stage either way.
    # "Cosmetic Repair" targets cosmetic_received (the cosmetic pipeline's
    # holding stage before Cleaning — see routers/cosmetic.py
    # COSMETIC_PIPELINE), not cleaning directly, matching the Final QC Fail
    # (Bucket) table's "move to Cosmetic Received" requirement.
    "L1/L2 Repair": "l1", "Stress Test": "qc_check", "Cosmetic Repair": "cosmetic_received",
}
DEPT_TO_ROLE = {
    "L1 Engineer": "l1_engineer", "L2 Engineer": "l2_engineer",
    "L1/L2 Repair": "l1_engineer", "Stress Test": "qc_inspector", "Cosmetic Repair": "cosmetic_manager",
}
STAGE_ENUM = {"l1": DeviceStage.l1, "qc_check": DeviceStage.qc_check, "cleaning": DeviceStage.cleaning,
              "cosmetic_received": DeviceStage.cosmetic_received}

# WorkOrder.stage is VARCHAR(5) (models/work_order.py) — DEPT_TO_STAGE's own
# values ("qc_check", "cosmetic_received") are the DeviceStage/STAGE_ENUM key,
# not a storable WorkOrder code, and are too long to write there directly
# (silent DBAPIError: StringDataRightTruncationError the first time an
# engineer is actually attached to a Stress Test / Cosmetic Repair move).
# Reuses the same short codes routers/cosmetic.py already writes/reads for
# these same destinations (MOVE_STAGE_CODE / FAIL_REASON_SOURCE_STAGE_CODE)
# so a WorkID looks the same regardless of which flow created it.
DEPT_TO_WO_STAGE_CODE = {
    "L1 Engineer": "l1", "L2 Engineer": "l1",
    "L1/L2 Repair": "l1", "Stress Test": "qc", "Cosmetic Repair": "recv",
}


async def _gen_work_id(db: AsyncSession) -> str:
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


# ── API helpers (GET — no CSRF check) ────────────────────────────────────────

@router.get("/api/buckets")
async def list_buckets(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: str = "stock_in",
    with_stage: str = "",
    exempt_customer_return: str = "",
):
    """`with_stage` (opt-in) drops any bucket holding zero active devices in that
    stage, AND makes the returned `device_count` ("Bucket Qty") reflect only
    devices at that stage too — rather than every active device ever linked
    to the bucket, which double-counts an earlier, unrelated intake that
    happened to reuse the same bucket. The Production Manager's Bucket
    Allocation table passes trc_production so both the list and the Bucket
    Qty column only ever reflect devices that are actually in TRC Production;
    every other caller omits it and sees the unscoped count instead.

    `exempt_customer_return` (opt-in, only meaningful alongside with_stage)
    additionally keeps any is_customer_return bucket in the result even with
    zero devices at with_stage — its tags are mid-repair at whatever stage
    their return re-entered them at, never with_stage itself when that's
    stock_in, so the plain with_stage filter would otherwise hide it.
    Inventory Manager's own Buckets/Cartons table (with_stage=stock_in) is
    the one caller that wants this; left off by default so Production
    Manager's Bucket Allocation tab keeps hiding empty buckets exactly as
    before."""
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    rows = (await db.execute(
        select(Bucket).where(Bucket.status.in_(statuses)).order_by(Bucket.created_at.desc())
    )).scalars().all()

    if not rows:
        return JSONResponse([])

    want_stage = None
    if with_stage.strip():
        try:
            want_stage = DeviceStage(with_stage.strip())
        except ValueError:
            raise HTTPException(400, f"Unknown stage {with_stage!r}")

    bucket_ids = [b.id for b in rows]
    if want_stage is not None:
        count_rows = (await db.execute(
            select(Device.bucket_id, func.count(Device.id))
            .where(Device.bucket_id.in_(bucket_ids),
                   Device.current_stage == want_stage, Device.is_active == True, Device.is_trashed == False)
            .group_by(Device.bucket_id)
        )).all()
        count_map = {str(r[0]): r[1] for r in count_rows}
        # Only buckets with at least one device actually at `want_stage` —
        # unless exempt_customer_return opted in and this bucket is one.
        rows = [b for b in rows
                if count_map.get(str(b.id), 0) > 0
                or (exempt_customer_return and b.is_customer_return)]
        if not rows:
            return JSONResponse([])
        bucket_ids = [b.id for b in rows]

        # A bucket kept only via exempt_customer_return has 0 in count_map
        # (that's what let it through) — replace its Qty with its real,
        # unscoped device count so the table doesn't show a misleading 0 for
        # a bucket that plainly has tags, just not at want_stage.
        exempt_ids = [b.id for b in rows
                     if count_map.get(str(b.id), 0) == 0 and exempt_customer_return and b.is_customer_return]
        if exempt_ids:
            unscoped_rows = (await db.execute(
                select(Device.bucket_id, func.count(Device.id))
                .where(Device.bucket_id.in_(exempt_ids), Device.is_active == True, Device.is_trashed == False)
                .group_by(Device.bucket_id)
            )).all()
            count_map.update({str(r[0]): r[1] for r in unscoped_rows})
    else:
        count_rows = (await db.execute(
            select(Device.bucket_id, func.count(Device.id))
            .where(Device.bucket_id.in_(bucket_ids), Device.is_active == True, Device.is_trashed == False)
            .group_by(Device.bucket_id)
        )).all()
        count_map = {str(r[0]): r[1] for r in count_rows}

    # Total Pass / Total Fail — count of tag numbers per bucket by Final QC
    # decision (Device.final_qc_status), shown as extra Movement table columns
    # on both the Inventory Manager and Production Manager pages.
    qc_rows = (await db.execute(
        select(Device.bucket_id, Device.final_qc_status, func.count(Device.id))
        .where(Device.bucket_id.in_(bucket_ids), Device.is_active == True, Device.is_trashed == False,
               Device.final_qc_status.isnot(None))
        .group_by(Device.bucket_id, Device.final_qc_status)
    )).all()
    pass_map, fail_map = {}, {}
    for bkt_id, qc_status, cnt in qc_rows:
        target = pass_map if qc_status == "pass" else fail_map
        target[str(bkt_id)] = target.get(str(bkt_id), 0) + cnt

    loc_map = {}
    loc_ids = {b.location_id for b in rows if b.location_id}
    if loc_ids:
        loc_rows = (await db.execute(
            select(StorageLocation).where(StorageLocation.id.in_(loc_ids))
        )).scalars().all()
        loc_map = {l.id: l for l in loc_rows}

    return JSONResponse([{
        "id": str(b.id),
        "bucket_number": b.bucket_number,
        "name": b.name or "",
        "location": b.location or "",
        "location_unit_id": loc_map[b.location_id].unit_id if b.location_id in loc_map else "",
        "location_type": loc_map[b.location_id].unit_type_label if b.location_id in loc_map else "",
        "category": b.category or "",
        "status": b.status,
        "device_count": count_map.get(str(b.id), 0),
        "received_qty": b.received_qty,
        "assigned_to_production": bool(b.assigned_to_production),
        "dept_assigned": bool(b.dept_assigned),
        "is_customer_return": bool(b.is_customer_return),
        "total_pass": pass_map.get(str(b.id), 0),
        "total_fail": fail_map.get(str(b.id), 0),
    } for b in rows])


@router.get("/api/buckets/device-map")
async def bucket_device_map(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: str = "stock_in",
):
    """Returns {barcode: bucket_number} for all devices in buckets of given status."""
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    buckets = (await db.execute(
        select(Bucket).where(Bucket.status.in_(statuses))
    )).scalars().all()
    if not buckets:
        return JSONResponse({})

    bucket_num_map = {b.id: b.bucket_number for b in buckets}
    devices = (await db.execute(
        select(Device.barcode, Device.bucket_id)
        .where(Device.bucket_id.in_(list(bucket_num_map.keys())), Device.is_active == True, Device.is_trashed == False)
    )).all()
    return JSONResponse({d.barcode: bucket_num_map[d.bucket_id] for d in devices})


@router.get("/api/buckets/{bucket_id}/tags")
async def bucket_tags(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    devices = (await db.execute(
        select(Device).where(Device.bucket_id == uid, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()
    # Latest Final-QC failure note per device — failure reasons live only in
    # StageMovement notes, and the Production Manager needs to see them here.
    fail_notes = {}
    dev_ids = [d.id for d in devices]
    if dev_ids:
        mv_rows = (await db.execute(
            select(StageMovement.device_id, StageMovement.notes)
            .where(StageMovement.device_id.in_(dev_ids),
                   StageMovement.notes.like("Final QC Failed%"))
            .order_by(StageMovement.moved_at.desc())
        )).all()
        for _did, _note in mv_rows:
            fail_notes.setdefault(_did, _note)
    return JSONResponse([{
        "barcode": d.barcode,
        "brand": d.brand or "",
        "model": d.model or "",
        "grade": d.grade.value if d.grade else "",
        "final_qc_status": d.final_qc_status or "",
        "failure_note": fail_notes.get(d.id, ""),
    } for d in devices])


@router.get("/api/bucket-engineers")
async def bucket_engineers(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns engineers for the Assign Bucket modal's Select Engineer dropdown —
    L1/L2 Repair, Stress Test (QC Handler), and Cosmetic Repair (Cosmetic
    Manager) roles, filtered client-side by whichever radio is selected."""
    rows = (await db.execute(
        select(User).where(
            User.role.in_([UserRole.l1_engineer, UserRole.l2_engineer,
                           UserRole.qc_inspector, UserRole.cosmetic_manager]),
            User.status == True,
        ).order_by(User.full_name)
    )).scalars().all()
    return JSONResponse([{
        "id": str(u.id),
        "name": u.full_name or u.username,
        "role": str(u.role),
    } for u in rows])


@router.get("/api/production-manager")
async def production_manager(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the current Production Manager for the Assign-to-Production modal.
    Judgment call: this system has no dedicated 'production_manager' UserRole —
    routers/stock.py already treats UserRole.inventory_manager as the Production
    Manager role for TRC/bucket operations (see `allowed = require_roles(admin,
    inventory_manager)` at the top of stock.py and buckets.py), so we reuse it
    here rather than inventing a new role that would require a schema-review
    approval for a new enum value."""
    mgr = (await db.execute(
        select(User).where(User.role == UserRole.inventory_manager, User.status == True)
        .order_by(User.full_name).limit(1)
    )).scalar_one_or_none()
    if not mgr:
        return JSONResponse({"id": None, "name": None})
    return JSONResponse({"id": str(mgr.id), "name": mgr.full_name or mgr.username})


@router.get("/api/bucket-warehouses")
async def bucket_warehouses(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wh = (await db.execute(
        select(MasterData.value)
        .where(MasterData.category == "warehouse", MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )).scalars().all()
    return JSONResponse(list(wh) or FALLBACK_WAREHOUSES)


@router.get("/api/buckets/lookup-by-name")
async def lookup_bucket_by_name(name: str = "", db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(allowed)):
    """Inventory Manager's 'Add to Bucket / Carton' modal — as the operator
    types a Bucket Name, check whether it matches an EXISTING bucket (a
    physical carton being reused), case-insensitive exact match. If it does,
    the modal shows how many tags are still mapped to that bucket right now,
    so the operator can decide whether to Clear it (POST
    /buckets/{id}/release, already used elsewhere for exactly this) before
    adding the newly selected tags — see create_bucket below, which reuses
    this same bucket by name rather than creating a duplicate."""
    name = (name or "").strip()
    if not name:
        return JSONResponse({"found": False})
    bucket = (await db.execute(
        select(Bucket).where(func.lower(Bucket.name) == name.lower())
    )).scalars().first()
    if not bucket:
        return JSONResponse({"found": False})
    tag_count = (await db.execute(
        select(func.count(Device.id)).where(Device.bucket_id == bucket.id, Device.is_active == True, Device.is_trashed == False)
    )).scalar() or 0
    return JSONResponse({
        "found": True, "bucket_id": str(bucket.id), "bucket_number": bucket.bucket_number,
        "name": bucket.name, "tag_count": tag_count,
    })


# ── Write endpoints (POST — CSRF verified) ───────────────────────────────────

@router.post("/buckets/create")
async def create_bucket(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    barcodes: str = Form(...),
    name: str = Form(default=""),
    location: str = Form(default=""),
    location_id: str = Form(default=""),
):
    barcode_list = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not barcode_list:
        raise HTTPException(400, "No barcodes provided")

    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(barcode_list), Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()
    if not devices:
        raise HTTPException(404, "No matching devices found")

    # Derive category from first device brand
    category = devices[0].brand if devices else None

    loc_uuid = None
    loc = None
    if location_id and location_id.strip():
        try:
            loc_uuid = uuid.UUID(location_id.strip())
        except Exception:
            loc_uuid = None
        if loc_uuid:
            loc = (await db.execute(
                select(StorageLocation).where(StorageLocation.id == loc_uuid)
            )).scalar_one_or_none()

    # A physical carton gets reused — typing the same Bucket Name as an
    # existing bucket (case-insensitive exact match) adds these tags to that
    # SAME bucket instead of creating a confusingly-duplicate one with the
    # same label. See /api/buckets/lookup-by-name above, which the modal
    # calls as the operator types, offering a "Clear Bucket" action
    # (POST /buckets/{id}/release) first if the old tags shouldn't carry
    # over. Whether or not they cleared it, reusing by name here is what
    # makes "Clear Bucket" meaningful — without this, clearing would just
    # empty a bucket that a fresh /buckets/create call would abandon anyway.
    name = name.strip()
    bucket = None
    reused = False
    if name:
        bucket = (await db.execute(
            select(Bucket).where(func.lower(Bucket.name) == name.lower())
        )).scalars().first()
        reused = bucket is not None

    if not bucket:
        bucket = Bucket(
            bucket_number=_new_bucket_number(),
            name=name or None,
            location=(loc.display_name if loc else (location.strip() or None)),
            location_id=loc.id if loc else None,
            category=category,
            status="stock_in",
            created_by=current_user.username,
        )
        db.add(bucket)
        await db.flush()
    elif loc:
        # Reusing an existing bucket for a new batch — its location may have
        # changed since it was last filled (a carton can move racks between
        # uses), so the newly-picked location wins.
        bucket.location = loc.display_name
        bucket.location_id = loc.id
        bucket.updated_at = app_now()

    for d in devices:
        d.bucket_id = bucket.id
        if loc:
            d.location_id = loc.id
        # Task 2(c): mark devices as Stock Inward stage on bucket assignment
        d.current_stage = DeviceStage.stock_in
        d.updated_at = app_now()
        await ensure_stage_location(db, d, DeviceStage.stock_in, current_user)

    await db.commit()
    return JSONResponse({
        "ok": True, "bucket_number": bucket.bucket_number, "bucket_id": str(bucket.id),
        "count": len(devices), "reused": reused,
    })


@router.post("/buckets/{bucket_id}/edit")
async def edit_bucket(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    name: str = Form(default=""),
    location: str = Form(default=""),
):
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")
    bucket.name = name.strip() or bucket.name
    bucket.location = location.strip() or bucket.location
    bucket.updated_at = app_now()
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/buckets/{bucket_id}/rename")
async def rename_bucket(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    name: str = Form(default=""),
):
    """Inventory Manager bucket table 'Edit' action — renames the bucket only.
    Kept separate from /buckets/{id}/edit because that endpoint treats a blank
    field as 'keep existing', so it can never clear a name; this one sets the
    value explicitly (blank → NULL), which the name-only modal needs."""
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    old_name = bucket.name
    bucket.name = name.strip() or None
    bucket.updated_at = app_now()
    await audit(
        db, action="BUCKET_RENAMED", user=current_user,
        table_name="buckets", record_id=str(bucket.id),
        old_value={"name": old_name}, new_value={"name": bucket.name},
        notes=f"Bucket {bucket.bucket_number} renamed", request=request,
    )
    await db.commit()
    return JSONResponse({"ok": True, "name": bucket.name or ""})


async def _move_bucket_devices_to_trc(db: AsyncSession, bucket: Bucket, username: str) -> int:
    """Move every active Stock In device in a bucket to DeviceStage.trc_production
    and write the StageMovement audit row for each.

    Shared by both routes that hand a bucket to Production, so the two cannot
    drift: /assign-to-production (Stock Inward Movement) and /move-to-trc
    (Inventory Manager's "Move to Production").

    Scoped to current_stage == stock_in on purpose: a bucket_number is reused
    across unrelated intakes over time, and without this filter, assigning a
    bucket to production here swept up every device that had EVER shared that
    bucket_id — including ones long since sold, scrapped, or mid-repair from a
    previous batch — and yanked them back to trc_production. Reported as
    "old mapped tags" getting reset when a bucket picked fresh on the Final QC
    page happened to be one reused from an earlier intake.

    is_customer_return buckets (Return Stock's Assign Bucket, routers/stock.py)
    are exempt from the stock_in restriction: their devices are mid-repair
    tags re-entering the pipeline at whatever stage they were returned to
    (iqc, l1, cleaning, ...) — never stock_in — so the filter above matched
    zero devices for them, the bucket showed 0 tags at trc_production, and
    /api/buckets?with_stage=trc_production (which drops buckets with zero
    active devices in that stage) silently omitted it from the Bucket
    Allocation tab even though Move to Production had "succeeded". These
    buckets don't carry the stale-reused-bucket-number risk the stock_in
    filter guards against — every device on one got there specifically via
    that same Assign Bucket action, never an unrelated earlier intake.
    """
    filters = [Device.bucket_id == bucket.id, Device.is_active == True, Device.is_trashed == False]
    if not bucket.is_customer_return:
        filters.append(Device.current_stage == DeviceStage.stock_in)
    devices = (await db.execute(select(Device).where(*filters))).scalars().all()

    moved = 0
    for device in devices:
        prev_stage = device.current_stage
        prev_mv = (await db.execute(
            select(StageMovement).where(
                StageMovement.device_id == device.id,
                StageMovement.to_stage == prev_stage,
                StageMovement.exited_at == None,
            ).order_by(StageMovement.moved_at.desc())
        )).scalars().first()
        if prev_mv:
            prev_mv.exited_at = app_now()
        device.current_stage = DeviceStage.trc_production
        device.updated_at = app_now()
        db.add(StageMovement(
            device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.trc_production,
            moved_by=username,
            notes=f"Bucket {bucket.bucket_number} moved to Production by {username}",
        ))
        moved += 1
    return moved


@router.post("/buckets/{bucket_id}/move-to-trc")
async def move_bucket_to_trc(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")
    bucket.status = "trc_pending"
    bucket.updated_at = app_now()
    # Also mark allocated so it appears in the Inventory Manager's
    # "Allocation — Buckets in Production" table — that table filters on
    # assigned_to_production, and "Move to Production" is exactly the action
    # that should populate it.
    if not bucket.assigned_to_production:
        bucket.assigned_to_production = True
        bucket.assigned_to_production_by = current_user.username
        bucket.assigned_to_production_at = app_now()
    # Handing the bucket to Production must also move its devices onto the
    # Production Manager's stage — without this the bucket appears there but
    # its tag numbers are still sitting at stock_in.
    moved = await _move_bucket_devices_to_trc(db, bucket, current_user.username)
    await db.commit()
    return JSONResponse({"ok": True, "moved": moved})


@router.post("/buckets/{bucket_id}/validate")
async def validate_bucket(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    qty_received: int = Form(...),
):
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")
    bucket.received_qty = qty_received
    bucket.status = "validated"
    bucket.updated_at = app_now()
    await db.commit()
    return JSONResponse({"ok": True})


async def _apply_department_move(
    db: AsyncSession, device: Device, department: str, engineer: User | None,
    current_user: User, bucket: Bucket | None = None,
) -> bool:
    """Move ONE device to the stage `department` implies (DEPT_TO_STAGE),
    optionally handing it to `engineer` via a fresh WorkOrder + notification.
    Shared by assign_bucket below (whole-bucket, `bucket` set — every active
    device sharing that bucket_id moves together) and Final QC's Devices
    Failed Move/Bulk Move (routers/cosmetic.py fqc_move_failed, `bucket=None`
    — a tag's own Bucket Name may hold siblings with a different Failure
    Reason since the 2026-08-27 bucket-lock removal, so that caller always
    moves exactly one tag at a time regardless of what else shares its
    bucket). Returns False (no-op, nothing logged) if `department` isn't a
    recognized destination."""
    target_stage = DEPT_TO_STAGE.get(department)
    new_stage = STAGE_ENUM.get(target_stage) if target_stage else None
    if not new_stage:
        return False

    _from_wh = getattr(device, "warehouse", None) or "—"
    transfer = StockTransfer(
        device_id=device.id,
        transfer_type="transfer_to_trc",
        # Not a /transfers/new submission — see StockTransfer.source's own
        # comment. Kept as a StockTransfer row for audit continuity, just
        # excluded from the /transfers page by default.
        source="department_assignment",
        from_warehouse=_from_wh,
        to_warehouse=_from_wh,
        transferred_by=current_user.username,
        department=department,
        barcode=device.barcode,
        serial_no=device.serial_no,
        make=device.brand,
        model=device.model,
        ram=str(device.ram_gb) + " GB" if device.ram_gb else None,
        hdd=str(device.storage_gb) + " GB" if device.storage_gb else None,
        category=device.sub_category,
        product_stage=device.current_stage.value if device.current_stage else None,
        transfer_date=app_now(),
        notes=(f"Assigned via Bucket {bucket.bucket_number}" if bucket
               else f"Final QC Fail — moved to {department}"),
        created_by=current_user.username,
    )
    db.add(transfer)
    await db.flush()

    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == prev_stage,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()

    device.current_stage = new_stage
    reset_repair_cycle_on_entry(device, prev_stage, new_stage)
    device.updated_at = app_now()
    if bucket:
        move_note = (f"Bucket {bucket.bucket_number} assigned to {engineer.full_name or engineer.username}"
                     if engineer else f"Bucket {bucket.bucket_number} moved to {department}")
    else:
        move_note = (f"Final QC Fail — assigned to {engineer.full_name or engineer.username}"
                     if engineer else f"Final QC Fail — moved to {department}")
    db.add(StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=new_stage,
        moved_by=current_user.username, notes=move_note,
    ))
    if engineer:
        # Going to Stress Test with an open, unassigned STRS- WorkID from
        # L1/L2 Mark Complete: give that one to the engineer instead of
        # raising a second Stress ID (2026-10-17).
        reused_wo = await reuse_open_stress_work_order(
            db, device.id, new_stage, engineer, created_by=current_user.username,
            source_transfer_id=transfer.id)
        await close_open_work_orders(db, device.id, keep_id=reused_wo.id if reused_wo else None)   # previous assignee's WorkID completes now
        if reused_wo:
            work_id = reused_wo.work_id
        else:
            work_id = await _gen_work_id(db)
            db.add(WorkOrder(
                work_id=work_id, device_id=device.id, barcode=device.barcode,
                stage=DEPT_TO_WO_STAGE_CODE.get(department, target_stage[:5]),
                assigned_role=DEPT_TO_ROLE.get(department),
                assigned_user_id=engineer.id, assigned_username=engineer.username,
                assigned_name=engineer.full_name, status="pending",
                source_transfer_id=transfer.id, created_by=current_user.username,
            ))
        _label = f"{device.brand or ''} {device.model or ''}".strip()
        if bucket:
            notify_msg = (f"{device.barcode}" + (f" ({_label})" if _label else "")
                          + f" assigned from Bucket {bucket.bucket_number} for {department} (WorkID: {work_id}).")
        else:
            notify_msg = (f"{device.barcode}" + (f" ({_label})" if _label else "")
                          + f" moved to {department} for Final QC rework and assigned to you (WorkID: {work_id}).")
        await create_notification(
            db, user_id=engineer.id,
            title="Device Assigned to You",
            message=notify_msg,
            notification_type="info",
            barcode=device.barcode,
            brand=device.brand,
            model=device.model,
            stage=new_stage.value if hasattr(new_stage, "value") else str(new_stage),
        )
    else:
        # No engineer named: the tag still arrives at its stage. At L1/L2,
        # L3/L4, Stress or Final QC that means one pending WorkID, to be
        # picked (2026-10-27); other stages: nothing.
        from services.stage_work_orders import open_for_stage as _open_for_stage
        await _open_for_stage(db, device, new_stage, created_by=current_user.username)
    return True


@router.post("/buckets/{bucket_id}/assign")
async def assign_bucket(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    department: str = Form(...),
    # Optional: the Final QC Fail (Bucket) table's Assign Bucket modal hides
    # "Select Engineer" for the "QC or Stress" / "Cosmetic Repair" radios —
    # those buckets just move stage, no per-device WorkID handoff (see the
    # "if engineer:" guard below). "L1/L2 Repair" still requires one, but
    # that's enforced client-side; the server accepts either shape.
    assigned_user_id: str = Form(default=""),
):
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    engineer = None
    if assigned_user_id:
        try:
            user_uid = uuid.UUID(assigned_user_id)
        except Exception:
            raise HTTPException(400, "Invalid user ID")
        engineer = (await db.execute(select(User).where(User.id == user_uid))).scalar_one_or_none()
        if not engineer:
            raise HTTPException(404, "Engineer not found")

    devices = (await db.execute(
        select(Device).where(Device.bucket_id == uid, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()

    for device in devices:
        await _apply_department_move(db, device, department, engineer, current_user, bucket=bucket)

    # Mark the bucket allocated so it appears in the Production Manager's
    # "Allocation — Buckets in L1/L2 Repair" table (that table filters on
    # assigned_to_production, the same flag the Inventory Manager side's
    # Assign-to-Production action sets — this endpoint is the L1/L2-specific
    # equivalent, so submitting this Assign Bucket modal should populate the
    # same allocation view).
    if devices and not bucket.assigned_to_production:
        bucket.assigned_to_production = True
        bucket.assigned_to_production_by = current_user.username
        bucket.assigned_to_production_at = app_now()

    # dept_assigned is the authoritative "this bucket has been handed to a
    # specific department/engineer" flag — Bucket Allocation tab excludes it,
    # Buckets in Repair Line requires it. Deliberately independent of device
    # stage (see the field's own comment in models/bucket.py).
    if devices:
        bucket.dept_assigned = True
        bucket.dept_assigned_by = current_user.username
        bucket.dept_assigned_at = app_now()

    await db.commit()
    return JSONResponse({"ok": True, "assigned": len(devices)})


@router.post("/devices/{barcode}/assign")
async def assign_device(
    barcode: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    department: str = Form(...),
    assigned_user_id: str = Form(...),
):
    """Production Manager's first table (item 23) — same Assign functionality
    and modal as the Allocation table's bucket-level Assign, scoped to one
    device instead of a whole bucket."""
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")

    try:
        user_uid = uuid.UUID(assigned_user_id)
    except Exception:
        raise HTTPException(400, "Invalid user ID")
    engineer = (await db.execute(select(User).where(User.id == user_uid))).scalar_one_or_none()
    if not engineer:
        raise HTTPException(404, "Engineer not found")

    target_stage = DEPT_TO_STAGE.get(department)
    new_stage = STAGE_ENUM.get(target_stage) if target_stage else None
    if not new_stage:
        raise HTTPException(400, "Invalid repair level")

    _from_wh = getattr(device, "warehouse", None) or "—"
    transfer = StockTransfer(
        device_id=device.id, transfer_type="transfer_to_trc",
        source="department_assignment",
        from_warehouse=_from_wh, to_warehouse=_from_wh,
        transferred_by=current_user.username, department=department,
        barcode=device.barcode, serial_no=device.serial_no,
        make=device.brand, model=device.model,
        ram=str(device.ram_gb) + " GB" if device.ram_gb else None,
        hdd=str(device.storage_gb) + " GB" if device.storage_gb else None,
        category=device.sub_category,
        product_stage=device.current_stage.value if device.current_stage else None,
        transfer_date=app_now(), notes=f"Assigned via Production Manager — {device.barcode}",
        created_by=current_user.username,
    )
    db.add(transfer)
    await db.flush()

    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == prev_stage,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()

    device.current_stage = new_stage
    reset_repair_cycle_on_entry(device, prev_stage, new_stage)
    device.updated_at = app_now()
    db.add(StageMovement(
        device_id=device.id, from_stage=prev_stage, to_stage=new_stage,
        moved_by=current_user.username,
        notes=f"Assigned to {engineer.full_name or engineer.username}",
    ))
    # Going to Stress Test with an open, unassigned STRS- WorkID from
    # L1/L2 Mark Complete: give that one to the engineer instead of
    # raising a second Stress ID (2026-10-17).
    reused_wo = await reuse_open_stress_work_order(
        db, device.id, new_stage, engineer, created_by=current_user.username,
        source_transfer_id=transfer.id)
    await close_open_work_orders(db, device.id, keep_id=reused_wo.id if reused_wo else None)   # previous assignee's WorkID completes now
    if reused_wo:
        work_id = reused_wo.work_id
    else:
        work_id = await _gen_work_id(db)
        db.add(WorkOrder(
            work_id=work_id, device_id=device.id, barcode=device.barcode,
            stage=target_stage, assigned_role=DEPT_TO_ROLE.get(department),
            assigned_user_id=engineer.id, assigned_username=engineer.username,
            assigned_name=engineer.full_name, status="pending",
            source_transfer_id=transfer.id, created_by=current_user.username,
        ))
    _label = f"{device.brand or ''} {device.model or ''}".strip()
    await create_notification(
        db, user_id=engineer.id,
        title="Device Assigned to You",
        message=(f"{device.barcode}" + (f" ({_label})" if _label else "")
                 + f" assigned for {department} (WorkID: {work_id})."),
        notification_type="info",
        barcode=device.barcode, brand=device.brand, model=device.model,
        stage=new_stage.value if hasattr(new_stage, "value") else str(new_stage),
    )
    await db.commit()
    return JSONResponse({"ok": True, "work_id": work_id})


@router.post("/devices/bulk-assign-l1l2")
async def bulk_assign_devices_l1l2(
    barcodes: str = Form(...),
    department: str = Form(...),
    assigned_user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Production Manager's Tag Number Allocation tab — "Bulk Assign": every
    checked tag is handed to one engineer and moved to L1/L2 Repair in one
    call. Same per-device logic as the single-device Assign above
    (assign_device), just looped over an arbitrary barcode list instead of
    one barcode — kept as its own loop (rather than reusing
    _apply_department_move, which is tuned for the bucket/Final-QC-fail
    contexts and would mislabel this one) so the StockTransfer/StageMovement
    notes stay accurate to what actually happened here."""
    codes = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not codes:
        raise HTTPException(400, "No tags selected")

    try:
        user_uid = uuid.UUID(assigned_user_id)
    except Exception:
        raise HTTPException(400, "Invalid user ID")
    engineer = (await db.execute(select(User).where(User.id == user_uid))).scalar_one_or_none()
    if not engineer:
        raise HTTPException(404, "Engineer not found")

    target_stage = DEPT_TO_STAGE.get(department)
    new_stage = STAGE_ENUM.get(target_stage) if target_stage else None
    if not new_stage:
        raise HTTPException(400, "Invalid repair level")

    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(codes))
    )).scalars().all()
    if not devices:
        raise HTTPException(404, "No matching tags found")

    for device in devices:
        _from_wh = getattr(device, "warehouse", None) or "—"
        transfer = StockTransfer(
            device_id=device.id, transfer_type="transfer_to_trc",
            source="department_assignment",
            from_warehouse=_from_wh, to_warehouse=_from_wh,
            transferred_by=current_user.username, department=department,
            barcode=device.barcode, serial_no=device.serial_no,
            make=device.brand, model=device.model,
            ram=str(device.ram_gb) + " GB" if device.ram_gb else None,
            hdd=str(device.storage_gb) + " GB" if device.storage_gb else None,
            category=device.sub_category,
            product_stage=device.current_stage.value if device.current_stage else None,
            transfer_date=app_now(), notes=f"Bulk assigned via Production Manager — {device.barcode}",
            created_by=current_user.username,
        )
        db.add(transfer)
        await db.flush()

        prev_stage = device.current_stage
        prev_mv = (await db.execute(
            select(StageMovement).where(
                StageMovement.device_id == device.id,
                StageMovement.to_stage == prev_stage,
                StageMovement.exited_at == None,
            ).order_by(StageMovement.moved_at.desc())
        )).scalars().first()
        if prev_mv:
            prev_mv.exited_at = app_now()

        device.current_stage = new_stage
        reset_repair_cycle_on_entry(device, prev_stage, new_stage)
        device.updated_at = app_now()
        db.add(StageMovement(
            device_id=device.id, from_stage=prev_stage, to_stage=new_stage,
            moved_by=current_user.username,
            notes=f"Bulk assigned to {engineer.full_name or engineer.username}",
        ))
        # Going to Stress Test with an open, unassigned STRS- WorkID from
        # L1/L2 Mark Complete: give that one to the engineer instead of
        # raising a second Stress ID (2026-10-17).
        reused_wo = await reuse_open_stress_work_order(
            db, device.id, new_stage, engineer, created_by=current_user.username,
            source_transfer_id=transfer.id)
        await close_open_work_orders(db, device.id, keep_id=reused_wo.id if reused_wo else None)   # previous assignee's WorkID completes now
        if reused_wo:
            work_id = reused_wo.work_id
        else:
            work_id = await _gen_work_id(db)
            db.add(WorkOrder(
                work_id=work_id, device_id=device.id, barcode=device.barcode,
                stage=target_stage, assigned_role=DEPT_TO_ROLE.get(department),
                assigned_user_id=engineer.id, assigned_username=engineer.username,
                assigned_name=engineer.full_name, status="pending",
                source_transfer_id=transfer.id, created_by=current_user.username,
            ))
        _label = f"{device.brand or ''} {device.model or ''}".strip()
        await create_notification(
            db, user_id=engineer.id,
            title="Device Assigned to You",
            message=(f"{device.barcode}" + (f" ({_label})" if _label else "")
                     + f" bulk assigned for {department} (WorkID: {work_id})."),
            notification_type="info",
            barcode=device.barcode, brand=device.brand, model=device.model,
            stage=new_stage.value if hasattr(new_stage, "value") else str(new_stage),
        )

    await db.commit()
    return JSONResponse({"ok": True, "assigned": len(devices)})


@router.post("/buckets/{bucket_id}/assign-to-production")
async def assign_bucket_to_production(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Batch D Task 3 — Stock Inward Movement table 'Assign to Production' action.
    Marks the bucket assigned_to_production=True and moves all its devices'
    stage to DeviceStage.trc_production (the existing stage value for the
    TRC Production Manager page, reused rather than inventing a new one)."""
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")
    if bucket.assigned_to_production:
        raise HTTPException(400, "Bucket already assigned to production")

    moved = await _move_bucket_devices_to_trc(db, bucket, current_user.username)

    bucket.assigned_to_production = True
    bucket.assigned_to_production_by = current_user.username
    bucket.assigned_to_production_at = app_now()
    bucket.status = "trc_pending"
    bucket.updated_at = app_now()

    await db.commit()
    return JSONResponse({"ok": True, "assigned": moved})


@router.post("/buckets/{bucket_id}/release")
async def release_bucket(
    bucket_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Inventory Manager's Final QC Pass (Buckets) table — 'Release Bucket'
    action. Removes the bucket_id link from every active device currently
    in this bucket; devices themselves are untouched (already ready_to_sale
    by the time a bucket reaches this table)."""
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    devices = (await db.execute(
        select(Device).where(Device.bucket_id == uid, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()
    for device in devices:
        device.bucket_id = None
        device.updated_at = app_now()
    await db.commit()
    return JSONResponse({"ok": True, "released": len(devices)})


@router.post("/buckets/{bucket_id}/assign-to-engineer")
async def assign_bucket_to_engineer(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Batch D Task 4 — Production Manager page Movement table 'Assign to
    Engineer' action. Moves all the bucket's devices into DeviceStage.l1 so
    they appear on templates/repair/l1.html's existing query
    (Device.current_stage == DeviceStage.l1), matching routers/repair.py's
    STAGE_MAP without any new plumbing on the L1 page."""
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    devices = (await db.execute(
        select(Device).where(Device.bucket_id == uid, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()

    for device in devices:
        prev_stage = device.current_stage
        prev_mv = (await db.execute(
            select(StageMovement).where(
                StageMovement.device_id == device.id,
                StageMovement.to_stage == prev_stage,
                StageMovement.exited_at == None,
            ).order_by(StageMovement.moved_at.desc())
        )).scalars().first()
        if prev_mv:
            prev_mv.exited_at = app_now()
        device.current_stage = DeviceStage.l1
        reset_repair_cycle_on_entry(device, prev_stage, DeviceStage.l1)
        device.updated_at = app_now()
        db.add(StageMovement(
            device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.l1,
            moved_by=current_user.username,
            notes=f"Bucket {bucket.bucket_number} assigned to L1 Engineer by {current_user.username}",
        ))
        # Arrived at L1/L2 with nobody named: one pending WorkID (2026-10-27).
        from services.stage_work_orders import open_for_stage as _open_for_stage
        await _open_for_stage(db, device, DeviceStage.l1, created_by=current_user.username)

    bucket.status = "validated"
    bucket.updated_at = app_now()

    await db.commit()
    return JSONResponse({"ok": True, "assigned": len(devices)})


@router.post("/buckets/{bucket_id}/release-repair-line")
async def release_bucket_from_repair_line(
    bucket_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Production Manager page, 'Buckets in Repair Line' — 'Release Bucket'
    (replaces the old 'Assign to Engineer' button there). Separate path from
    /buckets/{bucket_id}/release (Inventory Manager's Final QC Pass table,
    above) — same path would have shadowed that pre-existing endpoint, which
    only unmaps devices and never touches assigned_to_production/
    dept_assigned. Unmaps every active device currently linked to this
    bucket (bucket_id -> None) — devices keep whatever stage they're already
    at, this only removes the bucket association — and clears
    assigned_to_production/dept_assigned so the bucket drops out of both
    this table and the Bucket Allocation tab (both are filtered on
    assigned_to_production). Handed back to Inventory Manager's own bucket
    list, not deleted."""
    try:
        uid = uuid.UUID(bucket_id)
    except Exception:
        raise HTTPException(400, "Invalid bucket ID")
    bucket = (await db.execute(select(Bucket).where(Bucket.id == uid))).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    devices = (await db.execute(
        select(Device).where(Device.bucket_id == uid, Device.is_active == True, Device.is_trashed == False)
    )).scalars().all()

    for device in devices:
        device.bucket_id = None
        device.updated_at = app_now()

    bucket.assigned_to_production = False
    bucket.dept_assigned = False
    bucket.updated_at = app_now()

    await audit(db, action="BUCKET_RELEASED", user=current_user,
                table_name="buckets", record_id=str(bucket.id),
                new_value={"bucket_number": bucket.bucket_number, "released_count": len(devices)},
                request=request)

    await db.commit()
    return JSONResponse({"ok": True, "released": len(devices)})
