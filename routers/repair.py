"""
Repair Router - L1/L2/L3 with Control Engine, Cost Engine, Audit Engine, RepairAttempt
"""
from services.parts_changed import parts_badge_map
from templates_config import templates
import uuid as uuid_module
from datetime import datetime
from utils.timezone import app_now
from decimal import Decimal
from fastapi import APIRouter, Depends, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from sqlalchemy.exc import IntegrityError
from models.location import DeviceLocationLog, StorageLocation

from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, DeviceGrade, StageMovement, STAGE_LABELS, DROPDOWN_STAGES
from models.lot import Lot
from models.repair import RepairJob, RepairStatus
from models.engines import RepairAttempt, DeviceCosting
from auth.dependencies import get_current_user, require_roles, verify_csrf
from models.role_permissions import has_perm
from services.control_engine import validate_transition, validate_repair_level, get_allowed_next_stages, assert_device_in_stage
from services.cost_engine import check_scrap_decision, auto_scrap_device, refresh_parts_cost, SCRAP_WARNING_RATIO
from services.audit_engine import audit
from services.notifications import create_notification
from services.location_defaults import ensure_stage_location
from models.spare_parts import SparePartConsumption as SPC, SparePart
from models.work_order import WorkOrder
from models.part_request import PartRequest
from models.pna_part import DevicePNAPart
from models.iqc_inspection import IQCInspection
from models.bucket import Bucket
from services.parts_required import compute_required
from routers.transfers import _gen_work_id

router = APIRouter(prefix="/repair", tags=["repair"], dependencies=[Depends(verify_csrf)])

# "l2"/"l3" retired: the /repair/l2 and /repair/l3 pages are withdrawn, so the
# catch-all GET /{stage} 404s on them. L1 work now completes straight to Stress
# Test; deeper repair is handled by the separate /repair/l3l4 endpoints.
STAGE_MAP  = {"l1": DeviceStage.l1}

# Roles that see the entire repair queue instead of just their own work.
# Everyone else sees the tags assigned to them plus the unclaimed pool — an
# engineer has no reason to see a colleague's bench, but hiding unclaimed tags
# from everyone is how devices go missing: most tags in L1 have no work order
# yet, and a strict own-only filter would make them invisible to every engineer
# at once, visible only to a supervisor who happens to look.
#
# "production_manager" is NOT a role in this system — it is a module/page name.
# inventory_manager already serves as the Production Manager for TRC and bucket
# work (see the note in routers/buckets.py::production_manager), which is what
# actually grants that oversight here. The string is listed anyway so that if a
# custom role by that name is ever created in the permission matrix, it inherits
# the full queue without needing a code change.
FULL_QUEUE_ROLES = {"admin", "trc_manager", "inventory_manager", "production_manager"}


def _sees_full_queue(user) -> bool:
    """True for supervisors. Custom roles (trc_manager) are plain strings, not
    UserRole members, so read the value defensively rather than assuming an enum."""
    role = user.role.value if hasattr(user.role, "value") else str(user.role or "")
    return role in FULL_QUEUE_ROLES
# Forward move out of repair is always Stress Test. The old l1→l2→l3 ladder is gone:
# /repair/l1 is the merged L1/L2 queue and L3/L4 runs on WorkOrders, not stages.
# l2/l3 keys are kept so any legacy device still sitting in those stages can move on.
NEXT_STAGE = {DeviceStage.l1: DeviceStage.qc_check,
              DeviceStage.l2: DeviceStage.qc_check,
              DeviceStage.l3: DeviceStage.qc_check}
LEVEL_MAP  = {"l1": 1, "l2": 2, "l3": 3}


def stage_allowed(stage: str):
    role_map = {
        "l1": require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.l1_engineer,
                             UserRole.qc_inspector, UserRole.sales_manager),
        "l2": require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.l2_engineer,
                             UserRole.qc_inspector, UserRole.sales_manager),
        "l3": require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.l3_engineer,
                             UserRole.qc_inspector, UserRole.sales_manager),
    }
    return role_map.get(stage, require_roles(UserRole.admin))


# ── WorkID generation for the status-driven L1/L2 ↔ L3/L4 hand-off flow ────────
# work_orders.work_id is VARCHAR(12). "L3L4-"/"L1L2-" prefixes (5 chars) leave 7
# digits → 12 chars total, which fits the column without a widening migration.
async def _gen_prefixed_work_id(db: AsyncSession, prefix: str) -> str:
    width = 12 - len(prefix)
    base = (await db.execute(
        select(func.count(WorkOrder.id)).where(WorkOrder.work_id.like(f"{prefix}%"))
    )).scalar() or 0
    n = base + 1
    for _ in range(100000):
        wid = f"{prefix}{str(n).zfill(width)}"
        taken = (await db.execute(
            select(WorkOrder.id).where(WorkOrder.work_id == wid)
        )).scalar_one_or_none()
        if not taken:
            return wid
        n += 1
    raise HTTPException(500, "Could not allocate a WorkID")


async def _close_l1l2_work_orders(db: AsyncSession, device_id) -> None:
    """Mark every open L1/L2 WorkOrder of a tag completed, stamped now.

    Called whenever the tag leaves L1/L2 (job completed, scrapped, sent back to
    Stock Inward). Without it the WorkID stays "pending" forever, so WorkID
    Status shows no Completed Date for it and under-counts an engineer's day
    (found 2026-10-07: 1,306 of 1,638 open L1/L2 WorkIDs belonged to tags that
    had already moved on). Same predicate l1l2_complete_to_stress uses."""
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(WorkOrder)
        .where(WorkOrder.device_id == device_id,
               or_(WorkOrder.work_id.like("L1L2-%"), WorkOrder.stage.in_(["l1", "l2"])),
               WorkOrder.status != "completed")
        .values(status="completed", completed_at=app_now())
    )


async def _create_work_order_with_unique_id(db: AsyncSession, prefix: str, **fields) -> WorkOrder:
    """Generate a prefixed WorkID and insert the WorkOrder, retrying under a
    SAVEPOINT if a concurrent request already claimed the ID in between
    _gen_prefixed_work_id's existence check and this insert (found 2026-09-23:
    two L1/L2 engineers requesting L3/L4 within the same window could compute
    the identical "next" WorkID and both commit it, producing a real duplicate
    work_id across two different devices). The SAVEPOINT keeps the retry
    scoped to just the insert — it does not roll back the caller's earlier
    stage-change/StageMovement work already pending in the same session."""
    # Each retry only resolves once some other concurrent request commits (or
    # rolls back) the row it collided with, so the attempt budget needs to
    # cover realistic worst-case concurrency (a handful of engineers
    # submitting requests within the same second), not just 1-2 retries.
    for _attempt in range(20):
        work_id = await _gen_prefixed_work_id(db, prefix)
        wo = WorkOrder(work_id=work_id, **fields)
        try:
            async with db.begin_nested():
                db.add(wo)
                await db.flush()
            return wo
        except IntegrityError:
            continue
    raise HTTPException(500, "Could not allocate a unique WorkID after 20 attempts")


async def _close_open_movement(db, device):
    """Mark the device's current open StageMovement as exited (mirrors the
    existing pattern used throughout complete_repair)."""
    prev_mv = (await db.execute(
        select(StageMovement)
        .where(StageMovement.device_id == device.id,
               StageMovement.to_stage == device.current_stage,
               StageMovement.exited_at == None)
        .order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()


@router.post("/l1l2-start")
async def l1l2_start(
    request: Request,
    device_id: str = Form(...),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lightweight status-only Start Repair for the reworked L1/L2 page.

    Also creates a RepairJob for the device at its current L1/L2 stage so it
    surfaces in `open_jobs` — the "Complete L1/L2 Repair Job" side panel's
    Open-Job dropdown is populated from RepairJob rows, so without this the
    panel is unusable (BUG 1)."""
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    device.l1l2_status = "Repair Started"
    device.updated_at = app_now()

    # Stage label ("L1"/"L2") derived from the device's current stage — this is
    # exactly what repair_list's open_jobs query filters on (RepairJob.stage).
    stage_label = (device.current_stage.value if device.current_stage else "l1").upper()
    existing_job = (await db.execute(
        select(RepairJob).where(
            RepairJob.device_id == device.id,
            RepairJob.stage == stage_label,
            RepairJob.status != RepairStatus.completed,
        )
    )).scalars().first()
    if not existing_job:
        db.add(RepairJob(
            device_id=device.id, stage=stage_label,
            engineer_id=current_user.id, engineer_name=current_user.full_name,
            assigned_engineer=current_user.full_name,
            status=RepairStatus.in_progress,
        ))
    await audit(db, user=current_user, action="L1L2_REPAIR_STARTED",
                table_name="devices", record_id=str(device.id),
                notes=f"L1/L2 repair started on {device.barcode}", request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l1?success=Repair+started", status_code=302)


@router.post("/l1/pick")
async def l1_pick(
    request: Request,
    device_id: str = Form(...),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """"Pick This" — a tag sitting in the merged L1/L2 queue with no WorkID
    yet (unclaimed pool) gets a WorkOrder created for the CURRENT user with
    one click, no engineer-picker modal. Same WorkOrder shape as every other
    plain L1/L2 WorkID (stage "l1", _gen_work_id's 12-digit numeric ID — see
    routers/buckets.py's bulk_assign_devices_l1l2, the closest precedent),
    so it shows up in this page's own work_map and on /workid-status
    identically to an engineer-assigned pick. No StockTransfer: this isn't a
    location move. A same-stage (l1 -> l1) StageMovement IS written though
    (2026-09-18 fix), matching every other engineer-assignment flow's
    close-prior/open-new pattern (routers/stock.py's Change Engineer,
    bulk-assign, etc.) — without it, Pick This left no Tag Asset History
    entry at all, and /workid-status's Assigned Engineer column (sourced
    from the device's latest StageMovement, not WorkOrder.assigned_username)
    kept showing whoever performed the device's last unrelated stage move
    instead of the user who just picked it. No self-notification either —
    the user just clicked the button, they already know."""
    if not has_perm(current_user.role.value, "repair_l1", "add"):
        raise HTTPException(403, "You do not have permission to pick L1/L2 repairs")

    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    if device.current_stage != DeviceStage.l1:
        raise HTTPException(400, "Tag is not in the L1/L2 queue")

    existing = (await db.execute(
        select(WorkOrder.id).where(
            WorkOrder.device_id == device.id,
            WorkOrder.stage == "l1",
            WorkOrder.status != "completed",
        )
    )).scalar_one_or_none()
    if existing:
        # Already picked (double-click / two engineers racing the same row)
        # — no-op rather than a confusing error, the page will show whoever
        # got there first.
        return RedirectResponse(url="/repair/l1", status_code=302)

    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == DeviceStage.l1,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()

    work_id = await _gen_work_id(db)
    db.add(WorkOrder(
        work_id=work_id, device_id=device.id, barcode=device.barcode,
        stage="l1", assigned_role=current_user.role.value,
        assigned_user_id=current_user.id, assigned_username=current_user.username,
        assigned_name=current_user.full_name, status="pending",
        created_by=current_user.username,
    ))
    db.add(StageMovement(
        device_id=device.id, from_stage=DeviceStage.l1, to_stage=DeviceStage.l1,
        moved_by=current_user.username,
        notes=f"Picked by {current_user.full_name or current_user.username} (WorkID: {work_id})",
    ))

    await audit(db, user=current_user, action="L1L2_PICKED",
                table_name="devices", record_id=str(device.id),
                notes=f"{device.barcode} picked by {current_user.full_name or current_user.username} (WorkID: {work_id})",
                request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l1?success=Tag+picked", status_code=302)


@router.post("/bulk-part-request")
async def bulk_part_request(
    request: Request,
    part_label: str = Form(...),
    part_category: str = Form(""),
    request_type: str = Form("new"),
    part_name: str = Form(""),
    part_make: str = Form(""),
    part_model: str = Form(""),
    downgrade_type: str = Form(""),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk Part Request table on L1/L2 — raise one PartRequest per tag in the
    L1 queue that still needs `part_label`, instead of opening each tag.

    Scope matches the Tag Number table above it: active devices at stage l1.
    A tag that already has an open request for this part is skipped, so the
    action is safe to press twice.
    """
    devices = (await db.execute(
        select(Device).where(Device.current_stage == DeviceStage.l1, Device.is_active == True,
                              Device.is_trashed == False)
    )).scalars().all()
    if not devices:
        return RedirectResponse(url="/repair/l1?error=No+devices+in+the+queue", status_code=302)

    device_ids = [d.id for d in devices]
    iqc_rows = (await db.execute(
        select(IQCInspection).where(IQCInspection.device_id.in_(device_ids))
    )).scalars().all()
    iqc_by_dev = {}
    for iqc in iqc_rows:
        iqc_by_dev.setdefault(str(iqc.device_id), iqc)

    # Tags that already have a live request for this part — don't duplicate.
    existing = {
        str(did) for (did,) in (await db.execute(
            select(PartRequest.device_id).where(
                PartRequest.device_id.in_(device_ids),
                PartRequest.part_name == part_label,
                PartRequest.status.in_(["requested", "handed_over", "procure"]),
            )
        )).all()
    }

    # Resolve one SparePart for the whole batch, the same way the per-tag
    # modal's server-side fallback does.
    sp_match = None
    if part_name.strip():
        exact = select(SparePart).where(SparePart.name == part_name.strip())
        if part_make.strip():
            exact = exact.where(SparePart.make == part_make.strip())
        if part_model.strip():
            exact = exact.where(SparePart.model == part_model.strip())
        sp_match = (await db.execute(
            exact.order_by(SparePart.qty_in_stock.desc())
        )).scalars().first()
    if not sp_match:
        conds = [SparePart.name.ilike(f"%{part_label}%")]
        if part_category.strip():
            conds.append(SparePart.category == part_category.strip())
        sp_match = (await db.execute(
            select(SparePart).where(or_(*conds)).order_by(SparePart.qty_in_stock.desc())
        )).scalars().first()

    wo_rows = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id.in_(device_ids),
                                WorkOrder.status != "completed")
        .order_by(WorkOrder.assigned_at.desc())
    )).scalars().all()
    wo_by_dev = {}
    for wo in wo_rows:
        wo_by_dev.setdefault(str(wo.device_id), wo)

    raised = 0
    for dev in devices:
        did = str(dev.id)
        if did in existing:
            continue
        iqc = iqc_by_dev.get(did)
        needed = any(r["required"] and r["label"] == part_label
                     for r in compute_required(iqc, dev))
        if not needed:
            continue
        wo = wo_by_dev.get(did)
        db.add(PartRequest(
            work_order_id=wo.id if wo else None,
            work_id=wo.work_id if wo else None,
            device_id=dev.id, barcode=dev.barcode, stage="l1",
            part_id=sp_match.id if sp_match else None,
            part_name=part_label,
            part_category=part_category.strip() or None,
            request_type=(request_type or "new").strip(),
            downgrade_type=downgrade_type.strip() or None,
            part_make=part_make.strip() or None,
            part_model=part_model.strip() or None,
            requested_by=current_user.username,
            engineer_name=current_user.full_name,
            qty_requested=1, status="requested",
        ))
        raised += 1

    await audit(db, user=current_user, action="BULK_PART_REQUESTED",
                table_name="part_requests", record_id=part_label,
                new_value={"part": part_label, "request_type": request_type, "raised": raised},
                notes=f"Bulk {request_type} request for {part_label} on {raised} tag(s)",
                request=request)
    await db.commit()
    if not raised:
        return RedirectResponse(
            url="/repair/l1?success=No+new+requests+needed+for+" + part_label.replace(" ", "+"),
            status_code=302)
    return RedirectResponse(
        url=f"/repair/l1?success={raised}+request(s)+raised+for+" + part_label.replace(" ", "+"),
        status_code=302)


@router.post("/request-l3l4")
async def request_l3l4(
    request: Request,
    device_id: str = Form(...),
    assigned_l3l4_username: str = Form(...),
    repair_notes: str = Form(""),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Raise an L3/L4 request: device stays in L1/L2, a NEW (L3L4-prefixed)
    WorkOrder is created for the chosen L3/L4 engineer. The existing L1/L2
    WorkOrder is left untouched (a device can hold two open WorkOrders)."""
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    eng = (await db.execute(
        select(User).where(User.username == assigned_l3l4_username)
    )).scalar_one_or_none()
    if not eng:
        raise HTTPException(404, "Selected L3/L4 engineer not found")

    device.l1l2_status = "Requested to L3/L4"
    # Blank box = leave any existing note alone
    if repair_notes.strip():
        device.repair_notes = repair_notes.strip()
    device.updated_at = app_now()

    # Move the tag's actual stage to L3 so it drops out of /repair/l1's query
    # (Device.current_stage == DeviceStage.l1) while it's genuinely away at
    # L3/L4 — previously current_stage was never touched here, so a
    # requested-out tag kept sitting in the L1/L2 table showing a stale
    # Status badge and a "Start Repair" button it had no business offering.
    prev_stage = device.current_stage
    await _close_open_movement(db, device)
    device.current_stage = DeviceStage.l3
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.l3,
                         moved_by=current_user.username,
                         notes=f"Requested to L3/L4 — assigned to {eng.full_name or eng.username}"))

    wo = await _create_work_order_with_unique_id(
        db, "L3L4-", device_id=device.id, barcode=device.barcode,
        stage="l3", assigned_role="l3_engineer",
        assigned_user_id=eng.id, assigned_username=eng.username,
        assigned_name=eng.full_name, status="pending",
        requested_by_name=current_user.full_name or current_user.username,
        created_by=current_user.username,
    )
    work_id = wo.work_id
    await create_notification(
        db, user_id=eng.id, title="L3/L4 Repair Requested",
        message=(f"{device.barcode} was sent to you for L3/L4 repair by "
                 f"{current_user.full_name or current_user.username} (WorkID: {work_id})."),
        notification_type="info", barcode=device.barcode,
        brand=device.brand, model=device.model, stage="l3",
    )
    await audit(db, user=current_user, action="REQUEST_L3L4",
                table_name="work_orders", record_id=str(device.id),
                new_value={"work_id": work_id, "assigned": eng.username}, request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l1?success=Requested+to+L3/L4", status_code=302)


async def _mark_complete_open_parts(db: AsyncSession, device_id):
    """Every still-open part request on this device, split into the two
    groups Mark Complete resolves differently (2026-09-24) — Normal and
    Faulty request_type treated identically in both groups, since a Faulty
    request is just another PartRequest row, not a different table:

      verify — status 'handed_over': a physical part actually reached the
        engineer (qty_handed_over > 0) but was never confirmed installed.
        Real, uncosted transaction — safe to close as 'received' (same as
        clicking Verify by hand) and count its cost, same as any manual
        Verify does.

      cancel — status 'requested': nothing was ever handed over
        (qty_handed_over == 0) — the Spare Parts Manager never actioned it.
        Force-closing this to 'received' would fabricate a part-installed
        event that never happened, so it's soft-closed to 'cancelled'
        instead (the same status Spare Parts' own bulk-delete uses for
        "never fulfilled, don't lose the record" — routers/part_requests.py
        bulk_request_action) — no cost, request just stops sitting open on
        a tag that has already moved on.

    Shared by the Mark Complete precheck (what to warn about) and the
    completion endpoint itself (what to actually close) so the two can
    never disagree about which rows count."""
    rows = (await db.execute(
        select(PartRequest).where(PartRequest.device_id == device_id,
                                  PartRequest.status.in_(["handed_over", "requested"]))
    )).scalars().all()
    return ([pr for pr in rows if pr.status == "handed_over"],
            [pr for pr in rows if pr.status == "requested"])


@router.get("/l1l2-complete-precheck")
async def l1l2_complete_precheck(
    device_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark Complete's soft-warn step: tells the Mark Complete button, before
    it submits, which parts on this device are still open — so the engineer
    sees them named before Mark Complete silently resolves every one (see
    l1l2_complete_to_stress below)."""
    to_verify, to_cancel = await _mark_complete_open_parts(db, device_id)
    return JSONResponse({
        "to_verify": [{"part_name": pr.part_name, "qty": pr.qty_handed_over,
                       "faulty": pr.request_type == "faulty"} for pr in to_verify],
        "to_cancel": [{"part_name": pr.part_name,
                       "faulty": pr.request_type == "faulty"} for pr in to_cancel],
    })


@router.post("/l1l2-complete-to-stress")
async def l1l2_complete_to_stress(
    request: Request,
    device_id: str = Form(...),
    stress_engineer_username: str = Form(""),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Complete L1/L2: move the device to Stress Test (qc_check). An engineer is
    optional — with none the STRS- WorkOrder is still raised, unassigned, so the
    WorkID Status board keeps full traceability.

    Also resolves (2026-09-24) every still-open part request on this device
    — Normal or Faulty request_type alike — instead of leaving them open on
    a tag that has already moved past L1/L2. See _mark_complete_open_parts
    above for the two outcomes: 'handed_over' rows auto-verify (status ->
    'received', cost counted — routers/devices.py's changed_parts_consumed
    already sums every 'received' PartRequest regardless of request_type,
    so this needs no separate handling there); 'requested' rows auto-cancel
    (status -> 'cancelled', no cost — nothing was ever handed over).
    l1l2-complete-precheck (above) is what the Mark Complete button calls
    first to warn about exactly this set before it happens."""
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    eng = None
    if stress_engineer_username:
        eng = (await db.execute(
            select(User).where(User.username == stress_engineer_username)
        )).scalar_one_or_none()
        if not eng:
            raise HTTPException(404, "Selected Stress Test engineer not found")

    to_verify, to_cancel = await _mark_complete_open_parts(db, device.id)
    now = app_now()
    for pr in to_verify:
        pr.status = "received"
        pr.actioned_at = now
        pr.actioned_by = current_user.username
    for pr in to_cancel:
        pr.status = "cancelled"
        pr.actioned_at = now
        pr.actioned_by = current_user.username
    if to_verify:
        await audit(db, user=current_user, action="PART_AUTO_VERIFIED_ON_COMPLETE",
                    table_name="part_requests", record_id=str(device.id),
                    new_value={"barcode": device.barcode,
                               "parts": [{"part_name": pr.part_name, "request_type": pr.request_type}
                                         for pr in to_verify]},
                    request=request)
    if to_cancel:
        await audit(db, user=current_user, action="PART_AUTO_CANCELLED_ON_COMPLETE",
                    table_name="part_requests", record_id=str(device.id),
                    new_value={"barcode": device.barcode,
                               "parts": [{"part_name": pr.part_name, "request_type": pr.request_type}
                                         for pr in to_cancel]},
                    request=request)

    prev = device.current_stage
    await _close_open_movement(db, device)
    # Close any open L1/L2 WorkOrders for this device
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(WorkOrder)
        .where(WorkOrder.device_id == device.id,
               or_(WorkOrder.work_id.like("L1L2-%"), WorkOrder.stage.in_(["l1", "l2"])),
               WorkOrder.status != "completed")
        .values(status="completed", completed_at=app_now())
    )
    # Also close any open RepairJobs so the device drops out of the side
    # "Complete Job" panel's dropdown — otherwise a stale job entry lets the
    # engineer submit /repair/complete after the device already moved to
    # qc_check, which 409s with a STAGE CONFLICT.
    await db.execute(
        sa_update(RepairJob)
        .where(RepairJob.device_id == device.id,
               RepairJob.status == RepairStatus.in_progress)
        .values(status=RepairStatus.completed, completed_at=app_now())
    )
    device.current_stage = DeviceStage.qc_check
    device.l1l2_status = "Completed"
    device.updated_at = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=DeviceStage.qc_check,
                         moved_by=current_user.username,
                         notes=(f"L1/L2 completed — assigned to Stress Test ({eng.full_name or eng.username})"
                                if eng else "L1/L2 completed — moved to Stress Test (unassigned)")))
    # A WorkOrder makes the assignment visible on the WorkID Status board
    wo = await _create_work_order_with_unique_id(
        db, "STRS-", device_id=device.id, barcode=device.barcode,
        stage="qc", assigned_role="qc_inspector",
        assigned_user_id=eng.id if eng else None,
        assigned_username=eng.username if eng else None,
        assigned_name=eng.full_name if eng else None, status="pending",
        requested_by_name=current_user.full_name or current_user.username,
        created_by=current_user.username,
    )
    work_id = wo.work_id
    if eng:
        await create_notification(
            db, user_id=eng.id, title="Device Assigned for Stress Test",
            message=(f"{device.barcode} was moved to Stress Test and assigned to you by "
                     f"{current_user.full_name or current_user.username}."),
            notification_type="info", barcode=device.barcode,
            brand=device.brand, model=device.model, stage="qc_check",
        )
    await audit(db, user=current_user, action="L1L2_COMPLETE_TO_STRESS",
                table_name="devices", record_id=str(device.id),
                new_value={"assigned": eng.username if eng else None}, request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l1?success=Moved+to+Stress+Test", status_code=302)


@router.post("/back-to-production")
async def back_to_production(
    request: Request,
    device_id: str = Form(...),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """"Back to Inventory" (was "Back to Production") — after an L3/L4 scrap
    decision (Normal Scrap / Replacement Scrap), send the device straight to
    the "Tags Scrapped" page (/scrap-products, routers/scrap.py), which
    requires BOTH grade=scrap AND current_stage=scrap_for_sale. Previously
    this only set current_stage=scrapped (no grade change), which landed the
    device on Production Manager's "Scrap Products from Repair Line" table
    instead — an intermediate stop the tag no longer needs to make."""
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    prev = device.current_stage
    await _close_open_movement(db, device)
    # Close open RepairJobs so the device drops out of the Complete-Job panel
    # (a stale entry would 409 with STAGE CONFLICT after the device leaves L1/L2).
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(RepairJob)
        .where(RepairJob.device_id == device.id,
               RepairJob.status == RepairStatus.in_progress)
        .values(status=RepairStatus.completed, completed_at=app_now())
    )
    device.current_stage = DeviceStage.scrap_for_sale
    device.grade = DeviceGrade.scrap
    device.updated_at = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=DeviceStage.scrap_for_sale,
                         moved_by=current_user.username,
                         notes=f"Back to Inventory — {device.l34_status or 'Scrap'} from repair line"))
    await audit(db, user=current_user, action="BACK_TO_INVENTORY",
                table_name="devices", record_id=str(device.id),
                notes=f"{device.l34_status or 'Scrap'} → Tags Scrapped", request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l1?success=Sent+to+Tags+Scrapped", status_code=302)


# ── L3/L4 Repair page (WorkOrder-driven, per assigned engineer) ────────────────
@router.get("/l3l4", response_class=HTMLResponse)
async def l3l4_list(request: Request,
                    db: AsyncSession = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    full_queue = _sees_full_queue(current_user)
    stmt = (select(WorkOrder, Device, Lot.lot_number)
            .join(Device, WorkOrder.device_id == Device.id)
            .join(Lot, Device.lot_id == Lot.id, isouter=True)
            .where(WorkOrder.work_id.like("L3L4-%"),
                   WorkOrder.status != "completed",
                   Device.is_trashed == False,
                   # A device whose WorkOrder is still open but has already
                   # moved on (Ready to Sale, Scrap for Sale, ...) via some
                   # other action path is stale here, not a real L3/L4 item —
                   # only show devices genuinely still at this stage.
                   Device.current_stage == DeviceStage.l3)
            .order_by(WorkOrder.assigned_at.desc()))
    if not full_queue:
        stmt = stmt.where(WorkOrder.assigned_user_id == current_user.id)
    rows = (await db.execute(stmt)).all()
    # A device can end up with more than one open L3L4 WorkOrder (data drift,
    # not intended) — dedupe to one row per device, keeping the most
    # recently assigned one since rows are already ordered by assigned_at desc.
    _seen_device_ids: set = set()
    _deduped_rows = []
    for wo, dev, lot_number in rows:
        if dev.id in _seen_device_ids:
            continue
        _seen_device_ids.add(dev.id)
        _deduped_rows.append((wo, dev, lot_number))
    rows = _deduped_rows

    today = app_now().date()
    bucket_map: dict = {}
    bucket_ids = [d.bucket_id for _, d, _ in rows if d.bucket_id]
    if bucket_ids:
        bkt_rows = (await db.execute(select(Bucket).where(Bucket.id.in_(bucket_ids)))).scalars().all()
        bkt_by_id = {str(b.id): (b.name or b.bucket_number) for b in bkt_rows}
        for _, d, _ln in rows:
            if d.bucket_id:
                bucket_map[str(d.id)] = bkt_by_id.get(str(d.bucket_id), "")

    items = []
    for wo, dev, lot_number in rows:
        aging = max(0, (today - wo.assigned_at.date()).days) if wo.assigned_at else 0
        items.append({
            "work_id": wo.work_id,
            "assigned_date": wo.assigned_at,
            "device_id": str(dev.id),
            "barcode": dev.barcode,
            "status": dev.l34_status or "New",
            "bucket": bucket_map.get(str(dev.id), ""),
            "notes": wo.requested_by_name or "—",
            "repair_notes": dev.repair_notes or "—",
            "aging": aging,
        })

    return templates.TemplateResponse("repair/l3l4.html", {
        "request": request, "current_user": current_user,
        "items": items, "total": len(items),
        "pna_map": await _pna_map(db, [it["device_id"] for it in items]),
        "parts_badges": await parts_badge_map(db, [it["device_id"] for it in items]),
    })



async def _pna_map(db, device_ids):
    """{device_id: [part names marked PNA]} for the given tags.

    One query for the whole page rather than one per row — the L1/L2 and L3/L4
    queues routinely list a few hundred tags, and a per-row lookup is the kind
    of N+1 that only shows up in production against the Seoul database.
    """
    if not device_ids:
        return {}
    # Callers pass UUIDs (L1/L2) or their string form (L3/L4 builds plain dicts),
    # so normalise rather than relying on the driver to coerce.
    ids = []
    for did in device_ids:
        try:
            ids.append(did if isinstance(did, uuid_module.UUID) else uuid_module.UUID(str(did)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return {}
    rows = (await db.execute(
        select(DevicePNAPart.device_id, DevicePNAPart.part_name)
        .where(DevicePNAPart.device_id.in_(ids),
               DevicePNAPart.is_active.is_(True))
        .order_by(DevicePNAPart.part_name)
    )).all()
    out = {}
    for did, name in rows:
        out.setdefault(str(did), []).append(name)
    return out

@router.post("/l3l4-start")
async def l3l4_start(
    request: Request,
    device_id: str = Form(...),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(WorkOrder)
        .where(WorkOrder.device_id == device.id, WorkOrder.work_id.like("L3L4-%"),
               WorkOrder.status != "completed")
        .values(status="in_progress", started_at=app_now())
    )
    device.l34_status = "Repair Started"
    device.updated_at = app_now()
    await audit(db, user=current_user, action="L3L4_REPAIR_STARTED",
                table_name="devices", record_id=str(device.id),
                notes=f"L3/L4 repair started on {device.barcode}", request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l3l4?success=Repair+started", status_code=302)


@router.post("/l3l4-complete")
async def l3l4_complete(
    request: Request,
    device_id: str = Form(...),
    activity: str = Form(""),
    repair_notes: str = Form(""),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(WorkOrder)
        .where(WorkOrder.device_id == device.id, WorkOrder.work_id.like("L3L4-%"),
               WorkOrder.status != "completed")
        .values(status="completed", completed_at=app_now())
    )
    device.l34_status = "Completed"
    if activity.strip():
        device.l34_activity = activity.strip()
    if repair_notes.strip():
        device.repair_notes = repair_notes.strip()
    # L1/L2's own status column was still stuck on "Requested to L3/L4" once
    # L3/L4 finished — nothing told the L1/L2 queue the device was actually
    # back and awaiting further action there.
    device.l1l2_status = "Returned from L3/L4"
    device.updated_at = app_now()
    # Move the tag's stage back to L1 — request_l3l4 moved it to L3 while it
    # was away, so without this the row would stay hidden from /repair/l1
    # forever even though L3/L4 work is done and it needs a next action there.
    prev_stage = device.current_stage
    await _close_open_movement(db, device)
    device.current_stage = DeviceStage.l1
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.l1,
                         moved_by=current_user.username,
                         notes="L3/L4 completed — returned to L1/L2"))
    await audit(db, user=current_user, action="L3L4_COMPLETE",
                table_name="devices", record_id=str(device.id),
                notes=f"L3/L4 completed on {device.barcode}", request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l3l4?success=Repair+completed", status_code=302)


@router.post("/l3l4-scrap")
async def l3l4_scrap(
    request: Request,
    device_id: str = Form(...),
    scrap_type: str = Form(...),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if scrap_type not in ("Normal Scrap", "Replacement Scrap"):
        raise HTTPException(400, "Invalid scrap type")
    device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(WorkOrder)
        .where(WorkOrder.device_id == device.id, WorkOrder.work_id.like("L3L4-%"),
               WorkOrder.status != "completed")
        .values(status="completed", completed_at=app_now())
    )
    device.l34_status = scrap_type
    device.updated_at = app_now()
    # Same as l3l4_complete: move back to L1 so the row reappears on
    # /repair/l1, where the "Back to Inventory" button for a scrap decision
    # actually lives (repair/l1.html branches on l34_status there).
    prev_stage = device.current_stage
    await _close_open_movement(db, device)
    device.current_stage = DeviceStage.l1
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.l1,
                         moved_by=current_user.username,
                         notes=f"L3/L4 {scrap_type} — returned to L1/L2"))
    await audit(db, user=current_user, action="L3L4_SCRAP",
                table_name="devices", record_id=str(device.id),
                notes=f"{scrap_type} on {device.barcode}", request=request)
    await db.commit()
    return RedirectResponse(url="/repair/l3l4?success=Scrap+recorded", status_code=302)


@router.get("/{stage}", response_class=HTMLResponse)
async def repair_list(stage: str, request: Request,
                      db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    if stage not in STAGE_MAP:
        raise HTTPException(404)
    device_stage = STAGE_MAP[stage]

    result = await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage == device_stage, Device.is_active == True,
               Device.is_trashed == False)
        .order_by(Device.updated_at.desc())
    )
    devices = result.all()

    # ── Scope to this engineer's own bench ───────────────────────────────────
    # Only the MOST RECENT assignment counts. A device can carry several open
    # work orders, so a tag reassigned away from me leaves my older work order
    # behind; treating any assignment-to-me as ownership kept the tag on the
    # previous engineer's list forever. Latest assignment wins, full stop.
    # Filtering here, before the maps below, means every downstream count and
    # lookup is scoped automatically rather than each one needing to remember.
    if not _sees_full_queue(current_user):
        dev_ids = [d.id for d, _ in devices]
        current_owner: dict[str, object] = {}
        if dev_ids:
            wo_rows = (await db.execute(
                select(WorkOrder.device_id, WorkOrder.assigned_user_id)
                .where(WorkOrder.device_id.in_(dev_ids),
                       WorkOrder.stage == stage,
                       WorkOrder.status != "completed",
                       WorkOrder.assigned_user_id.isnot(None))
                # created_at breaks ties: two work orders can share an
                # assigned_at when a reassignment happens in the same second.
                .order_by(WorkOrder.device_id, WorkOrder.assigned_at.desc(),
                          WorkOrder.created_at.desc())
            )).all()
            for did, uid in wo_rows:
                current_owner.setdefault(str(did), uid)
        # A device with no open assignment is unclaimed and stays visible to
        # everyone — that is how work reaches a bench in the first place.
        devices = [(d, ln) for d, ln in devices
                   if current_owner.get(str(d.id), current_user.id) == current_user.id]

    total = len(devices)
    visible_ids = {str(d.id) for d, _ in devices}
    jobs_result = await db.execute(
        select(RepairJob, Device.barcode, Device.brand, Device.model)
        .join(Device, RepairJob.device_id == Device.id)
        .where(RepairJob.stage == stage.upper(), RepairJob.status != RepairStatus.completed,
               # Only devices still physically in this stage — a job whose device
               # already moved (stress fail, transfer, scrap) must not be offered
               # in the Complete-Job dropdown or /repair/complete 409s on it.
               Device.current_stage == device_stage, Device.is_active == True,
               Device.is_trashed == False)
        .order_by(RepairJob.started_at.desc())
    )
    open_jobs = jobs_result.all()
    # Same scope as the table above — otherwise the Complete-Job dropdown would
    # still list a colleague's device even though its row is hidden.
    open_jobs = [r for r in open_jobs if str(r.RepairJob.device_id) in visible_ids]

    # The Location column was removed from the L1/L2 table, so the per-device
    # location lookup that fed it is gone too. It was not cheap: the subquery
    # grouped the whole device_location_logs table before filtering.
    # (repair/l2.html and l3.html still reference location_map, but STAGE_MAP
    # only maps "l1", so neither template is reachable.)

    # ── Batch scrap-warning + suggest-QC (no N+1) ────────────────────────────
    device_ids = [d.id for d, _ in devices]
    scrap_warning_map: dict = {}
    suggest_qc_ids: set = set()

    if device_ids:
        costing_rows = (await db.execute(
            select(DeviceCosting).where(DeviceCosting.device_id.in_(device_ids))
        )).scalars().all()
        for costing in costing_rows:
            if (costing.expected_sale_value and costing.total_cost and
                    costing.total_cost >= costing.expected_sale_value * SCRAP_WARNING_RATIO):
                pct = int(costing.total_cost / costing.expected_sale_value * 100)
                scrap_warning_map[str(costing.device_id)] = (
                    f"Cost ₹{costing.total_cost:,.0f} is {pct}% of "
                    f"expected sale ₹{costing.expected_sale_value:,.0f} — consider scrapping"
                )

        open_device_ids = {str(j.device_id) for j, *_ in open_jobs}
        suggest_qc_ids = {str(did) for did in device_ids} - open_device_ids

    # Two columns of the in-stock parts, used only to answer "is this part
    # available?" below. This used to load 1,253 full SparePart objects and pass
    # them to the template as `available_parts` — which repair/l1.html never
    # referenced. repair/l3l4.html used to run its own version of that same
    # heavier query for its on-page Request Part modal; the modal is gone
    # (2026-09-09 — Request Part is now a plain link to the device page,
    # matching L1/L2) and so is that query.
    in_stock_parts = (await db.execute(
        select(SparePart.category, SparePart.name).where(SparePart.qty_in_stock > 0)
    )).all()

    # ── Work orders (WorkID + assignment + timeline) for assigned devices ─────
    wo_by_device: dict = {}
    if device_ids:
        wo_rows = (await db.execute(
            select(WorkOrder).where(
                WorkOrder.device_id.in_(device_ids),
                WorkOrder.stage == stage,
                WorkOrder.status != "completed",
            ).order_by(WorkOrder.assigned_at.desc())
        )).scalars().all()
        for wo in wo_rows:
            key = str(wo.device_id)
            if key in wo_by_device:
                continue  # keep the latest (rows ordered desc)
            wo_by_device[key] = wo

    # ── Part-request 'not in stock' alerts to surface on the Pending list (#12) ─
    parts_alert_map: dict = {}
    if device_ids:
        na_rows = (await db.execute(
            select(PartRequest.device_id).where(
                PartRequest.device_id.in_(device_ids),
                PartRequest.status == "not_in_stock",
            )
        )).all()
        for (did,) in na_rows:
            parts_alert_map[str(did)] = True

    # ── Parts Required (IQC-driven count) + Parts Requested (requested + handed over) ─
    parts_required_map: dict = {}
    parts_requested_map: dict = {}
    if device_ids:
        # Required count: from each device's IQC inspection via the fixed parts matrix
        iqc_rows = (await db.execute(
            select(IQCInspection).where(IQCInspection.device_id.in_(device_ids))
        )).scalars().all()
        iqc_by_dev = {}
        for iqc in iqc_rows:
            iqc_by_dev.setdefault(str(iqc.device_id), iqc)  # one per device
        dev_by_id = {str(d.id): d for d, _ in devices}
        for did_str, dev in dev_by_id.items():
            iqc = iqc_by_dev.get(did_str)
            parts_required_map[did_str] = sum(
                1 for r in compute_required(iqc, dev) if r["required"]
            )
        # Requested count: PartRequests in requested / handed_over state
        pr_rows = (await db.execute(
            select(PartRequest.device_id, func.count(PartRequest.id))
            .where(PartRequest.device_id.in_(device_ids),
                   PartRequest.status.in_(["requested", "handed_over"]))
            .group_by(PartRequest.device_id)
        )).all()
        for did, cnt in pr_rows:
            parts_requested_map[str(did)] = cnt

    # ── Bulk Part Request table ──────────────────────────────────────────────
    # Rolls the per-device Parts Consumption rows up to one row per part name
    # across every tag currently shown in the Tag Number table above, so an
    # engineer can raise one request for the whole queue instead of opening
    # each tag. Counts mirror exactly what Device Detail shows per tag:
    #   Total Quantity  — tags where compute_required() says Required = Yes
    #   Total Requested — requests at 'requested' OR 'handed_over' (any OPEN
    #                     request not yet confirmed received) — 2026-09-14:
    #                     previously counted 'handed_over' only, so a request
    #                     just raised here (status='requested') didn't move
    #                     this badge at all. Looking unrequested right after
    #                     being requested is exactly what led staff to
    #                     re-request the same tags through Device Detail
    #                     instead, creating real duplicate PartRequest rows —
    #                     the New/Replace Request buttons below are now also
    #                     disabled once Total Requested reaches Total
    #                     Quantity, which depends on this count being live.
    #   Total Changed   — requests at 'received'   (the Part Changed state)
    bulk_parts: list = []
    if device_ids:
        required_counts: dict = {}
        # Every part gets a row, not just the ones IQC flagged Required — an
        # engineer finds faults at the bench the inspection never recorded, and
        # a part missing from this table cannot be requested for the queue at
        # all. Total Quantity still counts only the tags where the part IS
        # Required, so the column keeps its meaning and a part nobody needs
        # simply shows 0.
        for did_str, dev in dev_by_id.items():
            iqc = iqc_by_dev.get(did_str)
            for row in compute_required(iqc, dev):
                entry = required_counts.setdefault(
                    row["label"],
                    {"label": row["label"], "category": row["category"],
                     "qty": 0, "requested": 0, "changed": 0},
                )
                if row["required"]:
                    entry["qty"] += 1

        if required_counts:
            status_rows = (await db.execute(
                select(PartRequest.part_name, PartRequest.status, func.count(PartRequest.id))
                .where(PartRequest.device_id.in_(device_ids),
                       PartRequest.status.in_(["requested", "handed_over", "received"]))
                .group_by(PartRequest.part_name, PartRequest.status)
            )).all()
            for pname, pstatus, cnt in status_rows:
                entry = required_counts.get(pname)
                if not entry:
                    continue
                if pstatus in ("requested", "handed_over"):
                    entry["requested"] += cnt
                elif pstatus == "received":
                    entry["changed"] += cnt

        # Downgrade is only meaningful for the two upgradeable components,
        # matching the same condition Device Detail uses for its button.
        for entry in required_counts.values():
            entry["can_downgrade"] = entry["label"] in ("RAM", "Hard Drive")
        bulk_parts = sorted(required_counts.values(), key=lambda e: e["label"])

    # ── Per-device parts breakdown, embedded as JSON for the Tag table's
    # filter bar (search/CPU/RAM/HDD/Lot/PNA): filtering the Tag table client-
    # side also needs to recompute the Bulk Part Request table above to match
    # only the currently-visible tags, without a server round trip. Reuses
    # exactly the same compute_required() + PartRequest status data as
    # bulk_parts, just kept per-device instead of pre-aggregated, so the two
    # can never silently drift out of sync with each other.
    device_parts_required: dict = {}
    if device_ids:
        for did_str, dev in dev_by_id.items():
            iqc = iqc_by_dev.get(did_str)
            device_parts_required[did_str] = [
                {"label": r["label"], "category": r["category"], "required": bool(r["required"]),
                 "requested": False, "changed": False}
                for r in compute_required(iqc, dev)
            ]
        pr_status_rows = (await db.execute(
            select(PartRequest.device_id, PartRequest.part_name, PartRequest.status)
            .where(PartRequest.device_id.in_(device_ids),
                   PartRequest.status.in_(["requested", "handed_over", "received"]))
        )).all()
        for did, pname, pstatus in pr_status_rows:
            for entry in device_parts_required.get(str(did), []):
                if entry["label"] == pname:
                    if pstatus in ("requested", "handed_over"):
                        entry["requested"] = True
                    elif pstatus == "received":
                        entry["changed"] = True

    # ── Timeline pause/resume (item 32): a device is "blocked" while any of
    # its required parts currently has 0 live stock in Part Master. While
    # blocked, the WorkOrder's elapsed-days clock freezes; once every required
    # part has stock again, it resumes counting from today (past pause time is
    # banked in paused_days_total so it's never counted against the engineer).
    work_map: dict = {}
    if device_ids and wo_by_device:
        now = app_now()
        today = now.date()
        # Answered from the single in_stock_parts list rather than a query per
        # (category, keyword). PARTS_MATRIX asks at most 20 distinct questions,
        # so this was 20 round-trips a page — and originally one per part per
        # device, ~1000 of them, which is why the page stopped loading at all.
        # Same predicate as the old SQL: category match OR keyword substring of
        # the name, case-insensitive. Restricting the list to qty_in_stock > 0
        # is equivalent to the old "highest-stock match is non-zero" because the
        # column is NOT NULL and no part carries negative stock.
        stock_cache: dict = {}

        def _has_stock(category, keyword):
            key = (category, keyword)
            if key not in stock_cache:
                kw = (keyword or "").lower()
                stock_cache[key] = any(
                    cat == category or (kw and kw in (name or "").lower())
                    for cat, name in in_stock_parts
                )
            return stock_cache[key]

        for did_str, dev in dev_by_id.items():
            wo = wo_by_device.get(did_str)
            if not wo:
                continue
            iqc = iqc_by_dev.get(did_str)
            blocked = False
            for row in compute_required(iqc, dev):
                if not row["required"]:
                    continue
                if not _has_stock(row["category"], row["keyword"]):
                    blocked = True
                    break

            if blocked and not wo.paused_since:
                wo.paused_since = now
            elif not blocked and wo.paused_since:
                wo.paused_days_total += max(0, (now - wo.paused_since).days)
                wo.paused_since = None

            paused_days = wo.paused_days_total + (
                max(0, (now - wo.paused_since).days) if wo.paused_since else 0
            )
            raw_days = (today - wo.assigned_at.date()).days if wo.assigned_at else 0
            work_map[did_str] = {
                "work_id": wo.work_id,
                "assigned_name": wo.assigned_name or wo.assigned_username or "—",
                "days_pending": max(0, raw_days - paused_days),
                "paused": bool(wo.paused_since),
                # Drives the Date Assigned column and the queue's default sort.
                "assigned_at": wo.assigned_at,
            }
        await db.commit()

    # ── Returned-device job ids (item 7): L3 shows a "Replace Device with" field
    #    only for jobs whose device has Return Status = Yes ─────────────────────
    returned_job_ids: set = set()
    open_dev_ids = [j.device_id for j, *_ in open_jobs]
    if open_dev_ids:
        ret_rows = (await db.execute(
            select(Device.id).where(Device.id.in_(open_dev_ids),
                                    Device.return_status == True)
        )).all()
        ret_dev = {str(r[0]) for r in ret_rows}
        returned_job_ids = {str(j.id) for j, *_ in open_jobs if str(j.device_id) in ret_dev}

    # ── Bucket map: device_id → bucket_number ────────────────────────────────
    bucket_map: dict = {}
    bucket_ids = [d.bucket_id for d, _ in devices if d.bucket_id]
    if bucket_ids:
        bkt_rows = (await db.execute(
            select(Bucket).where(Bucket.id.in_(bucket_ids))
        )).scalars().all()
        bkt_by_id = {str(b.id): (b.name or b.bucket_number) for b in bkt_rows}
        for device, _ in devices:
            if device.bucket_id:
                bucket_map[str(device.id)] = bkt_by_id.get(str(device.bucket_id), "")

    # ── Engineer dropdowns for the L1/L2 hand-off modals ──────────────────────
    l3l4_engineers = (await db.execute(
        select(User).where(User.role == "l3_engineer", User.status == True)
        .order_by(User.full_name)
    )).scalars().all()
    stress_engineers = (await db.execute(
        select(User).where(User.role == "qc_inspector", User.status == True)
        .order_by(User.full_name)
    )).scalars().all()

    return templates.TemplateResponse(f"repair/{stage}.html", {
        "request": request, "devices": devices, "open_jobs": open_jobs,
        "parts_badges": await parts_badge_map(db, [d.id for d, *_ in devices]),
        "l3l4_engineers": l3l4_engineers, "stress_engineers": stress_engineers,
        "stage": stage.upper(), "current_user": current_user,
        "returned_job_ids": returned_job_ids,
        "scrap_warning_map": scrap_warning_map,
        "suggest_qc_ids": suggest_qc_ids,
        "work_map": work_map,
        "parts_alert_map": parts_alert_map,
        "parts_required_map": parts_required_map,
        "parts_requested_map": parts_requested_map,
        "pna_map": await _pna_map(db, device_ids),
        "bulk_parts": bulk_parts,
        "device_parts_required": device_parts_required,
        # Feeds the Bulk Part Request modals' Name -> Make -> Model cascade,
        # the same shape Device Detail passes to its own request modals.
        "all_spare_parts": [
            {"id": str(sp.id), "name": sp.name, "category": sp.category,
             "make": sp.make, "model": sp.model}
            for sp in (await db.execute(
                select(SparePart).where(SparePart.is_trashed == False)
                .order_by(SparePart.category, SparePart.name)
            )).scalars().all()
        ],
        "bucket_map": bucket_map,
        "started_ids": {str(j.device_id) for j, *_ in open_jobs},
        "total": total,
    })


@router.post("/start")
async def start_repair(
    request: Request,
    barcode: str = Form(...),
    stage: str = Form(...),
    issue_description: str = Form(""),
    problem_reported: str = Form(""),
    team_name: str = Form(""),
    assigned_engineer: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    device_stage = STAGE_MAP.get(stage.lower())
    if not device_stage:
        raise HTTPException(400, "Invalid repair stage")
    assert_device_in_stage(device, device_stage)
    level    = LEVEL_MAP.get(stage.lower(), 1)
    is_admin = current_user.role.value == "admin"

    # Check permission for this repair level
    repair_module = f"repair_l{level}"
    if not has_perm(current_user.role.value, repair_module, "add"):
        raise HTTPException(403, f"You do not have permission to add {repair_module} repairs")

    # NOTE: a hard "requested >= required parts" gate was tried here and on the
    # Start Repair button, but compute_required()'s fallback (no IQC record on
    # a device) treats nearly every part category as "required" — with no
    # devices in this dataset carrying an IQC record, that made Start Repair
    # permanently blocked for everything. Reverted; parts_required_map /
    # parts_requested_map are still shown as informational columns on the
    # list page so an engineer can see what's outstanding without being
    # locked out of starting the job.

    await validate_repair_level(device, level, db)
    await validate_transition(device, device_stage, db, override_admin=is_admin)

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

    device.current_stage = device_stage
    device.updated_at    = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=device_stage,
                         moved_by=current_user.username, notes=f"Started {stage.upper()} repair"))
    db.add(RepairJob(device_id=device.id, stage=stage.upper(),
                     engineer_id=current_user.id, engineer_name=current_user.full_name,
                     issue_description=issue_description or None,
                     problem_reported=problem_reported or None,
                     team_name=team_name or None,
                     # Assigned To defaults to whoever is starting this stage — the
                     # form doesn't expose an override input, so the current
                     # logged-in engineer is the sensible default per stage.
                     assigned_engineer=assigned_engineer or current_user.full_name,
                     status=RepairStatus.in_progress))
    await audit(db, user=current_user, action="REPAIR_STARTED",
                table_name="repair_jobs", record_id=str(device.id),
                notes=f"{stage.upper()} started on {barcode}", request=request)
    await db.commit()
    return RedirectResponse(url=f"/repair/{stage.lower()}?success=Repair+started", status_code=302)


@router.post("/complete")
async def complete_repair(
    request: Request,
    job_id: str = Form(...),
    resolution: str = Form(""),
    move_to_next: str = Form("no"),
    final_status: str = Form(""),
    cost: str = Form("0"),
    time_spent: str = Form(""),
    dust_cleaning: str = Form(""),
    cmos_battery_change: str = Form(""),
    thermal_paste: str = Form(""),
    ram_status: str = Form(""),
    ram_removed_gb: str = Form(""),
    ram_added_gb: str = Form(""),
    hdd_updated: str = Form(""),
    hdd_removed: str = Form(""),
    hdd_added: str = Form(""),
    action_taken: str = Form(""),
    problem_reported: str = Form(""),
    problem_observed: str = Form(""),
    scrap_reason: str = Form(""),
    received_from: str = Form(""),
    customer_internal: str = Form(""),
    replace_with_barcode: str = Form(""),
    part_ids: list[str] = Form(default=[]),
    part_qtys: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(RepairJob).where(RepairJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404)

    # Check permission to edit this repair level
    repair_module = f"repair_{job.stage}"
    if not has_perm(current_user.role.value, repair_module, "edit"):
        raise HTTPException(403, f"You do not have permission to edit {repair_module} repairs")

    job.status = RepairStatus.completed; job.completed_at = app_now()
    job.resolution = resolution or None; job.final_status = final_status or None
    job.dust_cleaning = dust_cleaning or None; job.cmos_battery_change = cmos_battery_change or None
    job.thermal_paste = thermal_paste or None; job.ram_status = ram_status or None
    job.ram_removed_gb = ram_removed_gb or None; job.ram_added_gb = ram_added_gb or None
    job.hdd_updated = hdd_updated or None; job.hdd_removed = hdd_removed or None
    job.hdd_added = hdd_added or None; job.action_taken = action_taken or None
    job.problem_reported = problem_reported or None; job.problem_observed = problem_observed or None
    job.scrap_reason = scrap_reason or None; job.received_from = received_from or None
    job.customer_internal = customer_internal or None

    stage = job.stage.lower()
    level = LEVEL_MAP.get(stage, 1)
    dev_result = await db.execute(select(Device).where(Device.id == job.device_id))
    device = dev_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device for repair job not found")
    expected_stage = STAGE_MAP.get(stage)
    if expected_stage:
        assert_device_in_stage(device, expected_stage)

    # ── L3 device replacement (items 7 & 8) ──────────────────────────────────
    # When a returned device is swapped for a good one, cross-reference both
    # devices' "Replaced" field so the trail is traceable from either tag.
    rwb = (replace_with_barcode or "").strip()
    if stage == "l3" and rwb and rwb != device.barcode:
        repl = (await db.execute(
            select(Device).where(Device.barcode == rwb)
        )).scalar_one_or_none()
        if repl:
            device.replaced = f"Replaced by {repl.barcode}"
            repl.replaced = f"Replaced from {device.barcode}"
            await audit(db, user=current_user, action="DEVICE_REPLACED",
                        table_name="devices", record_id=str(device.id),
                        new_value={"replaced_by": repl.barcode, "from": device.barcode},
                        request=request)

    prev_att = (await db.execute(
        select(RepairAttempt).where(RepairAttempt.device_id == device.id, RepairAttempt.level == level)
    )).scalars().all()
    attempt_no  = len(prev_att) + 1
    repair_cost = Decimal(cost) if cost and cost.strip() else Decimal("0")
    time_mins   = int(time_spent) if time_spent and time_spent.strip() else None
    outcome     = ("scrapped" if final_status == "Scrap" else
                   "resolved" if move_to_next == "yes" else "escalated")

    db.add(RepairAttempt(device_id=device.id, repair_job_id=job.id, level=level,
                         attempt_no=attempt_no, cost=repair_cost, time_spent=time_mins,
                         outcome=outcome, notes=resolution or None, created_by=current_user.username))
    await refresh_parts_cost(device, db)  # keep DeviceCosting.labour_cost current

    scrap_result = await check_scrap_decision(device, db, current_user.username)
    force_scrap  = scrap_result["scrap"]
    warn_msg     = scrap_result.get("reason", "") if scrap_result.get("warning") else ""

    if final_status == "Scrap" or force_scrap:
        reason = scrap_reason or scrap_result.get("reason", "Scrapped")
        prev_mv = (await db.execute(
            select(StageMovement)
            .where(StageMovement.device_id == device.id,
                   StageMovement.to_stage  == device.current_stage,
                   StageMovement.exited_at == None)
            .order_by(StageMovement.moved_at.desc())
        )).scalars().first()
        if prev_mv:
            prev_mv.exited_at = app_now()
        await auto_scrap_device(device, reason, db, current_user.username)
        await _close_l1l2_work_orders(db, device.id)
        await audit(db, user=current_user,
                    action="AUTO_SCRAP" if force_scrap else "MANUAL_SCRAP",
                    table_name="devices", record_id=str(device.id), notes=reason, request=request)

    elif final_status in ("Escalate to L3", "Escalate to L2",
                          "Request to L3", "Request to L2") and device:
        from sqlalchemy import update as sa_update
        # The l2/l3 stages are retired, so escalation no longer moves the device: L1 and L2
        # share the /repair/l1 queue, and L3/L4 is raised as a separate WorkOrder via
        # /repair/request-l3l4. Sending it to l2/l3 here would strand it in a stage with no page.
        escalate_to = DeviceStage.l1
        is_admin    = current_user.role.value == "admin"
        current     = device.current_stage
        if current != escalate_to:
            await validate_transition(device, escalate_to, db, override_admin=is_admin)
            prev_mv = (await db.execute(
                select(StageMovement)
                .where(StageMovement.device_id == device.id,
                       StageMovement.to_stage  == current,
                       StageMovement.exited_at == None)
                .order_by(StageMovement.moved_at.desc())
            )).scalars().first()
            if prev_mv:
                prev_mv.exited_at = app_now()
            db.add(StageMovement(device_id=device.id, from_stage=current, to_stage=escalate_to,
                                 moved_by=current_user.username,
                                 notes=f"{job.stage} escalated — {final_status}"))
            device.current_stage = escalate_to
        # Close any other open repair jobs at this stage
        await db.execute(
            sa_update(RepairJob)
            .where(RepairJob.device_id == device.id,
                   RepairJob.stage     == job.stage,
                   RepairJob.status    == RepairStatus.in_progress)
            .values(status=RepairStatus.completed, completed_at=app_now())
        )
        device.l1l2_status = "New"
        device.updated_at  = app_now()
        # Carry the same WorkID forward (re-stage the open WorkOrder)
        await db.execute(
            sa_update(WorkOrder)
            .where(WorkOrder.device_id == device.id, WorkOrder.status != "completed")
            .values(stage=escalate_to.value)
        )

    elif final_status == "PNA" and device:
        # Parts Not Available — keep device in current stage; job status already saved above
        await audit(db, user=current_user, action="REPAIR_PNA",
                    table_name="repair_jobs", record_id=str(job.id),
                    new_value={"stage": job.stage, "barcode": device.barcode,
                               "status": "PNA", "resolution": resolution or ""},
                    request=request)

    elif final_status == "Back to Inventory" and device:
        # Move the device back to the Stock Inward table
        from sqlalchemy import update as sa_update
        current = device.current_stage
        prev_mv = (await db.execute(
            select(StageMovement)
            .where(StageMovement.device_id == device.id,
                   StageMovement.to_stage  == current,
                   StageMovement.exited_at == None)
            .order_by(StageMovement.moved_at.desc())
        )).scalars().first()
        if prev_mv:
            prev_mv.exited_at = app_now()
        # Close any other open repair jobs at this stage
        await db.execute(
            sa_update(RepairJob)
            .where(RepairJob.device_id == device.id,
                   RepairJob.stage     == job.stage,
                   RepairJob.status    == RepairStatus.in_progress)
            .values(status=RepairStatus.completed, completed_at=app_now())
        )
        db.add(StageMovement(device_id=device.id, from_stage=current, to_stage=DeviceStage.stock_in,
                             moved_by=current_user.username,
                             notes=f"{job.stage} completed — moved back to Stock Inward"))
        device.current_stage = DeviceStage.stock_in
        device.updated_at    = app_now()
        await _close_l1l2_work_orders(db, device.id)
        await ensure_stage_location(db, device, DeviceStage.stock_in, current_user)
        await create_notification(
            db,
            user_id=None,
            title="Device Returned to Stock Inward",
            message=(
                f"{device.barcode} was moved back to Stock Inward from {current.value} "
                f"by {current_user.full_name or current_user.username}."
            ),
            notification_type="warning",
            barcode=device.barcode,
            brand=device.brand,
            model=device.model,
            stage=DeviceStage.stock_in.value,
        )

    elif (final_status.strip() == "Completed" and device
          and device.current_stage in (DeviceStage.l1, DeviceStage.l2)):
        # Side "Complete Job" panel on the L1/L2 pages. A Completed L1/L2 job
        # moves the tag straight to Stress Test (qc_check). Movement bookkeeping
        # mirrors l1l2_complete_to_stress; unlike that endpoint this path
        # deliberately does NOT pick a Stress Test engineer and does NOT create a
        # STRS- WorkOrder — the tag lands unassigned in the Stress Test queue.
        # assert_device_in_stage already ran above against the job's own stage.
        from sqlalchemy import update as sa_update
        current = device.current_stage
        await _close_open_movement(db, device)
        # Close any other open repair jobs at this stage before moving
        await db.execute(
            sa_update(RepairJob)
            .where(RepairJob.device_id == device.id,
                   RepairJob.stage     == job.stage,
                   RepairJob.status    == RepairStatus.in_progress)
            .values(status=RepairStatus.completed, completed_at=app_now())
        )
        device.current_stage = DeviceStage.qc_check
        device.l1l2_status   = "Completed"
        device.updated_at    = app_now()
        await _close_l1l2_work_orders(db, device.id)
        db.add(StageMovement(device_id=device.id, from_stage=current,
                             to_stage=DeviceStage.qc_check,
                             moved_by=current_user.username,
                             notes=f"{job.stage} completed — moved to Stress Test"))
        await create_notification(
            db, user_id=None, title="Device Stage Changed",
            message=(f"{device.barcode} moved from {current.value} to "
                     f"{DeviceStage.qc_check.value} by "
                     f"{current_user.full_name or current_user.username}."),
            notification_type="alert", barcode=device.barcode,
            brand=device.brand, model=device.model,
            stage=DeviceStage.qc_check.value,
        )

    elif (move_to_next == "yes"
          or final_status.strip().lower() in ("completed", "ok")) and device:
        # A successful completion always advances to the next stage
        # (L1→L2, L2→L3, L3→QC) — the checkbox is a manual override for the
        # case where final_status is left blank.
        from sqlalchemy import update as sa_update
        is_admin = current_user.role.value == "admin"
        current  = device.current_stage
        # Per business rule: a Completed repair at L1/L2/L3 goes straight to Stress Test (qc_check)
        if final_status.strip().lower() in ("completed", "ok"):
            next_s = DeviceStage.qc_check
        else:
            next_s = NEXT_STAGE.get(current)
        if next_s:
            await validate_transition(device, next_s, db, override_admin=is_admin)
            prev_mv = (await db.execute(
                select(StageMovement)
                .where(StageMovement.device_id == device.id,
                       StageMovement.to_stage  == current,
                       StageMovement.exited_at == None)
                .order_by(StageMovement.moved_at.desc())
            )).scalars().first()
            if prev_mv:
                prev_mv.exited_at = app_now()
            # Close any other open repair jobs at this stage before moving
            await db.execute(
                sa_update(RepairJob)
                .where(RepairJob.device_id == device.id,
                       RepairJob.stage     == job.stage,
                       RepairJob.status    == RepairStatus.in_progress)
                .values(status=RepairStatus.completed, completed_at=app_now())
            )
            db.add(StageMovement(device_id=device.id, from_stage=current, to_stage=next_s,
                                 moved_by=current_user.username,
                                 notes=f"{job.stage} completed — {final_status or 'OK'}"))
            device.current_stage = next_s; device.updated_at = app_now()
            if current in (DeviceStage.l1, DeviceStage.l2):
                await _close_l1l2_work_orders(db, device.id)
            # Keep the L1/L2 status column consistent when completing via the
            # side Complete-Job panel (the old path predates l1l2_status).
            if current in (DeviceStage.l1, DeviceStage.l2):
                device.l1l2_status = "Completed"
            # Broadcast stage-change alert to all managers/admins (user_id=None = all users)
            await create_notification(
                db,
                user_id=None,
                title="Device Stage Changed",
                message=(
                    f"{device.barcode} moved from {current.value} to {next_s.value} "
                    f"by {current_user.full_name or current_user.username}."
                ),
                notification_type="alert",
                barcode=device.barcode,
                brand=device.brand,
                model=device.model,
                stage=next_s.value,
            )

    await audit(db, user=current_user, action="REPAIR_COMPLETE",
                table_name="repair_jobs", record_id=str(job.id),
                notes=f"{job.stage} complete — {outcome}", request=request)

    # Record spare parts used in this repair
    from decimal import Decimal as _Dec
    # Batch-fetch all referenced parts in one query
    import uuid as _uuid
    valid_part_ids = []
    for _pid in part_ids:
        try:
            valid_part_ids.append(_uuid.UUID(_pid))
        except (ValueError, AttributeError):
            pass

    _parts_map: dict = {}
    if valid_part_ids:
        _parts_res = await db.execute(
            select(SparePart).where(SparePart.id.in_(valid_part_ids))
        )
        _parts_map = {str(p.id): p for p in _parts_res.scalars().all()}

    for pid, qty_str in zip(part_ids, part_qtys):
        if not pid or not qty_str:
            continue
        try:
            qty = int(qty_str)
            if qty <= 0:
                continue
        except (ValueError, TypeError):
            continue
        _part = _parts_map.get(pid)
        if not _part:
            continue
        _part.qty_in_stock = max(0, (_part.qty_in_stock or 0) - qty)
        _total = _Dec(str(_part.unit_price)) * qty
        db.add(SPC(
            device_id=device.id if device else None,
            lot_id=device.lot_id if device else None,
            repair_job_id=job.id,
            part_id=_part.id,
            qty_used=qty,
            unit_cost=_part.unit_price,
            total_cost=_total,
            used_by=current_user.username,
            stage=job.stage,
            notes=f"Repair job {job.id}",
        ))

    await db.commit()
    import urllib.parse
    redirect = f"/repair/{stage}?success=Repair+completed"
    if warn_msg:
        redirect += f"&warning={urllib.parse.quote(warn_msg)}"
    return RedirectResponse(url=redirect, status_code=302)


@router.get("/move/form", response_class=HTMLResponse)
async def move_form(request: Request, barcode: str = None,
                    db: AsyncSession = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    device = None
    allowed_stages = []
    if barcode:
        r = await db.execute(select(Device).where(Device.barcode == barcode))
        device = r.scalar_one_or_none()
        if device:
            allowed_stages = await get_allowed_next_stages(device, db)
    return templates.TemplateResponse("repair/move.html", {
        "request": request, "current_user": current_user,
        "stages": DROPDOWN_STAGES, "stage_labels": STAGE_LABELS,
        "device": device, "allowed_stages": allowed_stages,
    })


@router.post("/move")
async def move_device(
    request: Request,
    barcode: str = Form(...),
    to_stage: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    is_admin  = current_user.role.value == "admin"
    new_stage = DeviceStage(to_stage)
    await validate_transition(device, new_stage, db, override_admin=is_admin)
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
    # Moving into Pass Hold / Ready to Sale without the Final QC Pass button
    # still writes verified RAM/Hard Drive changes onto the spec (2026-10-15).
    from services.parts_changed import apply_spec_changes_if_past_final_qc
    await apply_spec_changes_if_past_final_qc(db, device, prev, new_stage, current_user, request)
    device.current_stage = new_stage; device.updated_at = app_now()
    # A manual move off L3 bypasses l3l4_complete/l3l4_scrap, which are the
    # only two places that ever close an open "L3L4-" WorkOrder — without
    # this the WorkOrder is orphaned: it keeps showing on /repair/l3l4 and in
    # Production Manager's "Total Tags in L3/L4" tile's underlying queue,
    # disagreeing with the device's real (now different) stage forever.
    if prev == DeviceStage.l3 and new_stage != DeviceStage.l3:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(WorkOrder)
            .where(WorkOrder.device_id == device.id, WorkOrder.work_id.like("L3L4-%"),
                   WorkOrder.status != "completed")
            .values(status="completed", completed_at=app_now())
        )
    db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=new_stage,
                         moved_by=current_user.username, notes=notes or "Manual move"))
    await audit(db, user=current_user, action="STAGE_MOVED",
                table_name="devices", record_id=str(device.id),
                old_value={"stage": str(prev)}, new_value={"stage": to_stage}, request=request)
    await db.commit()
    return RedirectResponse(url=f"/repair/move/form?success=Device+moved+to+{to_stage}", status_code=302)
