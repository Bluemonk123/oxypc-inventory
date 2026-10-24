"""
Cosmetic Refurbishment Pipeline
Stages: QC Check → Cleaning → Dry Sanding → Masking → Painting → Water Sanding → Final QC → Ready to Sale
"""
from services.parts_changed import changed_parts_by_device, apply_spec_changes_on_pass, parts_badge_map
from templates_config import templates
from datetime import datetime
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, or_
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement
from models.stress import StressTestResult
from models.bucket import Bucket, _new_bucket_number
from models.lot import Lot
from models.iqc_inspection import IQCInspection
from models.repair import RepairJob
from models.part_request import PartRequest
from models.spare_parts import SparePart, SparePartConsumption
from services.parts_required import compute_required
from services.audit_engine import audit
from services.notifications import create_notification
from services.location_defaults import ensure_stage_location
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm
from models.work_order import WorkOrder
from models.role_permissions import has_perm, is_narrowly_scoped_to
from models.cosmetic_flow import CosmeticFlowRow
from models.cost_config import CostConfig
from models.master import EXTERNAL_PARTNER_TEST_ENTITY
from utils.master_data import entity_values
from utils.attendance_groups import is_group_manager, managed_usernames
from routers.transfers import _gen_work_id
from routers.buckets import _apply_department_move
import uuid as uuid_module

router = APIRouter(prefix="/cosmetic", tags=["cosmetic"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.qc_inspector,
                         UserRole.sales_manager, UserRole.cosmetic_manager)

# Ordered cosmetic pipeline — each stage advances to the next. Cosmetic
# Received is the holding stage a device lands in straight out of Stress
# Test (see routers/stress_api.py stress_complete_to_paint and
# routers/qc.py's pass branch) before cosmetic work actually starts; Cosmetic
# Completed is the equivalent holding stage once Water Sanding is done and
# before Final QC picks it up.
COSMETIC_PIPELINE = [
    DeviceStage.cosmetic_received,
    DeviceStage.cleaning,
    DeviceStage.putty,
    DeviceStage.dry_sanding,
    DeviceStage.masking,
    DeviceStage.painting,
    DeviceStage.water_sanding,
    DeviceStage.cosmetic_completed,
    DeviceStage.final_qc,
]

# Nav tab bar excludes Final QC — that page is reached via "Done & Move to
# Final QC" from Cleaning or by finishing the pipeline, not a direct jump.
COSMETIC_NAV_STAGES = [s for s in COSMETIC_PIPELINE if s != DeviceStage.final_qc]

NEXT_COSMETIC = {
    DeviceStage.qc_check:    DeviceStage.cosmetic_received,
    DeviceStage.cosmetic_received: DeviceStage.cleaning,
    DeviceStage.cleaning:    DeviceStage.putty,
    DeviceStage.putty:       DeviceStage.dry_sanding,
    DeviceStage.dry_sanding: DeviceStage.masking,
    DeviceStage.masking:     DeviceStage.painting,
    DeviceStage.painting:    DeviceStage.water_sanding,
    DeviceStage.water_sanding: DeviceStage.cosmetic_completed,
    DeviceStage.cosmetic_completed: DeviceStage.final_qc,
    DeviceStage.final_qc:    DeviceStage.ready_to_sale,
}

STAGE_LABELS = {
    DeviceStage.cosmetic_received: "Cosmetic Received",
    DeviceStage.cleaning:     "Cleaning",
    DeviceStage.putty:        "Putty",
    DeviceStage.dry_sanding:  "Dry Sanding",
    DeviceStage.masking:      "Masking",
    DeviceStage.painting:     "Painting",
    DeviceStage.water_sanding:"Water Sanding",
    DeviceStage.cosmetic_completed: "Cosmetic Completed",
    DeviceStage.final_qc:     "Final QC",
}

# WorkOrder.stage is VARCHAR(5) — short code per destination stage. Tags the
# WorkOrder created when a device is Moved INTO that stage (see advance_stage)
# and is what the WorkID column on that stage's own page looks up. "clean" was
# already in use (created at Stress Test Complete time) before this batch —
# repurposed here to mean "assigned for the Cleaning stage specifically",
# since that entry-level assignment now happens at Cosmetic Received instead
# (see routers/stress_api.py stress_complete_to_paint, code "recv").
MOVE_STAGE_CODE = {
    DeviceStage.cosmetic_received: "recv",
    DeviceStage.cleaning: "clean",
    DeviceStage.putty: "putty",
    DeviceStage.dry_sanding: "drysd",
    DeviceStage.masking: "mask",
    DeviceStage.painting: "paint",
    DeviceStage.water_sanding: "water",
    DeviceStage.cosmetic_completed: "comp",
    DeviceStage.final_qc: "fqc",
}

# Split Permission Matrix module key per cosmetic stage page (Master Data ->
# Module Permission). final_qc keeps its own pre-existing 'cosmetic_finalqc'
# key, untouched by this split.
PERM_MODULE_BY_STAGE = {
    DeviceStage.cosmetic_received: "cosmetic_received",
    DeviceStage.cleaning: "cosmetic_cleaning",
    DeviceStage.putty: "cosmetic_putty",
    DeviceStage.dry_sanding: "cosmetic_dry_sanding",
    DeviceStage.masking: "cosmetic_masking",
    DeviceStage.painting: "cosmetic_painting",
    DeviceStage.water_sanding: "cosmetic_water_sanding",
    DeviceStage.cosmetic_completed: "cosmetic_completed",
}

# Stages whose forward Move requires picking an assignee in a modal — every
# page in PERM_MODULE_BY_STAGE. The actual assignment requirement (see
# advance_stage below) additionally exempts any move that LANDS on Final QC
# regardless of source stage — Cosmetic Completed's normal "Move to Final QC"
# and Cosmetic Received's "skip cosmetic stages" button both land there.
# Final QC has its own page-level permission/access model (cosmetic_finalqc),
# not a per-device WorkID handoff, so those moves go straight through with no
# modal. Final QC's own Pass/Fail decisioning on this same /cosmetic/advance
# endpoint is a separate, pre-existing flow this doesn't touch.
ASSIGN_ON_MOVE_STAGES = set(PERM_MODULE_BY_STAGE.keys())

# The 6 mid-pipeline pages plus Cosmetic Received have the admin/Cosmetic
# Manager-only bulk "Assign" button — Cosmetic Completed is the one excluded
# (not asked for; it already has its own per-row Move/Fail assignment flow).
# Cosmetic Received's bulk-assign also advances to Cleaning (see bulk_assign
# below); the other 6 stay a pure reassignment.
BULK_ASSIGN_STAGES = {
    DeviceStage.cosmetic_received,
    DeviceStage.cleaning, DeviceStage.putty, DeviceStage.dry_sanding,
    DeviceStage.masking, DeviceStage.painting, DeviceStage.water_sanding,
}

# Roles eligible for the Move modal's assignee dropdown when the mover does
# not manage a Group Config team (Application Settings -> Group Config) —
# the same pool already used for the Stress Test "Complete" hand-off.
COSMETIC_ELIGIBLE_ROLES = (UserRole.cosmetic_manager, UserRole.qc_inspector,
                          UserRole.inventory_manager, UserRole.sales_manager)

# The 6 mid-pipeline "Flow Data" columns on All Tags — (field prefix on
# CosmeticFlowRow, display label, Permission Matrix module key). Same 6
# stages as PERM_MODULE_BY_STAGE minus Cosmetic Received/Completed, since
# those are hand-off holding stages, not a role a person works day to day.
FLOW_STAGE_COLUMNS = [
    ("cleaning", "Cleaning", "cosmetic_cleaning"),
    ("putty", "Putty", "cosmetic_putty"),
    ("dry_sanding", "Dry Sanding", "cosmetic_dry_sanding"),
    ("masking", "Masking", "cosmetic_masking"),
    ("painting", "Painting", "cosmetic_painting"),
    ("water_sanding", "Water Sanding", "cosmetic_water_sanding"),
]

# DeviceStage -> Flow Data field prefix, for the 6 stages above only.
FLOW_FIELD_BY_STAGE = {
    DeviceStage.cleaning: "cleaning",
    DeviceStage.putty: "putty",
    DeviceStage.dry_sanding: "dry_sanding",
    DeviceStage.masking: "masking",
    DeviceStage.painting: "painting",
    DeviceStage.water_sanding: "water_sanding",
}
_FLOW_MODULE_BY_FIELD = {field: module for field, _, module in FLOW_STAGE_COLUMNS}


async def _resolve_flow_next_user(db: AsyncSession, current_stage: DeviceStage,
                                   next_stage: DeviceStage, current_user_id) -> User | None:
    """Flow Data auto-assign (All Tags -> Flow Data): find a saved flow row
    where the person moving the tag sits in CURRENT stage's column, and
    return whoever sits in NEXT stage's column of that SAME row — the
    tag's next handler, per the flow this operator works in. Returns None
    (caller falls back to the manual "pick a user" modal, unchanged from
    before this feature) when no row matches, that row's next-stage cell is
    blank, or that user is no longer active / no longer permitted for the
    next stage — a stale Flow Data row must never block work, only skip the
    shortcut for that one move."""
    cur_field = FLOW_FIELD_BY_STAGE.get(current_stage)
    next_field = FLOW_FIELD_BY_STAGE.get(next_stage)
    if not cur_field or not next_field:
        return None
    cur_col = getattr(CosmeticFlowRow, f"{cur_field}_user_id")
    next_col = getattr(CosmeticFlowRow, f"{next_field}_user_id")
    row = (await db.execute(
        select(CosmeticFlowRow).where(cur_col == current_user_id, next_col.isnot(None))
        .order_by(CosmeticFlowRow.created_at)
    )).scalars().first()
    if not row:
        return None
    next_user_id = getattr(row, f"{next_field}_user_id")
    user = (await db.execute(
        select(User).where(User.id == next_user_id, User.status == True)
    )).scalar_one_or_none()
    if not user:
        return None
    role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
    if not has_perm(role_val, _FLOW_MODULE_BY_FIELD[next_field], "enable"):
        return None
    return user


# Roles that ALWAYS keep pre-existing hub/Group-Config behaviour, no matter
# what the Permission Matrix says about the 6 mid-pipeline stage modules —
# admin, cosmetic_manager, and the 3 general-purpose supervisor roles that
# were already allowed onto every cosmetic page (COSMETIC_ELIGIBLE_ROLES).
# Everyone else — a genuine single-stage custom role like "Cosmetic
# Cleaning" — is a "Cosmetic User": never sees the "Cosmetic & Paint" hub
# and is never treated as a page manager, regardless of Group Config.
_COSMETIC_HUB_ROLES = {"admin"} | {r.value for r in COSMETIC_ELIGIBLE_ROLES}


def _is_cosmetic_stage_role(role_val: str) -> bool:
    """True for a genuine single-stage cosmetic role (e.g. a custom
    "Cosmetic Cleaning" role) — anyone NOT in _COSMETIC_HUB_ROLES who was
    deliberately scoped to at least one of the 6 mid-pipeline stages (see
    models.role_permissions.is_narrowly_scoped_to). Mirrors the matching
    blacklist used for the sidebar's "Cosmetic & Paint" hub link
    (templates/base.html) — kept in sync so the nav link and the
    manager/member page behaviour below can never disagree.

    Fixed 2026-09-24: previously used has_perm() alone, which defaults to
    True for a module nobody ever configured either way. That misread a
    broad, deliberately-configured role like "trc_manager" (explicit rows
    across ~100 OTHER modules, but nobody had touched these 6 specifically)
    as a narrow single-stage worker, forcing manager_mode to False for them
    regardless of Group Config and hiding most stages/tags from someone
    who'd actually been set up as a manager. is_narrowly_scoped_to still
    falls back to the old permissive default for a role with ZERO matrix
    configuration anywhere — that unconfigured-role-defaults-to-narrow
    convention is intentional (see tests/test_cosmetic_page_sidebar_link.py)
    and unaffected by this fix."""
    if role_val in _COSMETIC_HUB_ROLES:
        return False
    return is_narrowly_scoped_to(role_val, *(module for _, _, module in FLOW_STAGE_COLUMNS))


async def _move_assignee_pool(db: AsyncSession, current_user: User) -> list:
    """Users offered in the Move modal's dropdown: the mover's own Group
    Config team (plus themselves) if they manage one, else the general
    cosmetic-eligible role pool. Mirrors the Fail modal's l1l2_engineers /
    Stress Test's allowed_paint_roles pattern."""
    team = await managed_usernames(db, current_user.username)
    if team:
        usernames = set(team) | {current_user.username}
        rows = (await db.execute(
            select(User).where(User.username.in_(usernames), User.status == True)
            .order_by(User.full_name)
        )).scalars().all()
    else:
        rows = (await db.execute(
            select(User).where(User.role.in_(COSMETIC_ELIGIBLE_ROLES), User.status == True)
            .order_by(User.full_name)
        )).scalars().all()
    return [{"id": str(u.id), "name": u.full_name or u.username} for u in rows]


# Final QC Fail "Devices Failed" — maps each of the 3 qc_failure_reason
# master-data values to the WorkOrder.stage code that identifies who most
# recently worked this tag through the stage the reason implies. Feeds the
# "Engineer Name" column AND (2026-08-29) is who the per-row "Move" /
# multi-select "Bulk Move" button hands the tag straight to — no modal.
FAIL_REASON_SOURCE_STAGE_CODE = {
    "Hardware": "l1",                                          # L1/L2
    "Software": "qc",                                          # Stress Test
    "Cosmetic": MOVE_STAGE_CODE[DeviceStage.cosmetic_completed],  # "comp"
}

# Same 3 reasons -> the Assign Bucket modal's own department labels
# (routers/buckets.py DEPT_TO_STAGE / DEPT_TO_ROLE / STAGE_ENUM) so Move /
# Bulk Move land on the exact same destination stage + WorkOrder role a
# human picking "L1/L2 Repair" / "QC or Stress" / "Cosmetic Repair" in that
# modal would have chosen.
FAIL_REASON_TO_DEPT = {
    "Hardware": "L1/L2 Repair",
    "Software": "Stress Test",
    "Cosmetic": "Cosmetic Repair",
}


async def _resolve_fail_engineer(db: AsyncSession, device_id, failure_reason: str) -> dict | None:
    """The most recently assigned user on this tag's own WorkOrder history
    at the stage `failure_reason` implies — same "latest WorkOrder wins"
    resolution every other stage page's engineer column already uses (see
    l1l2_engineer_map above). Returns None if the reason is unrecognized or
    no such WorkOrder exists yet (e.g. the tag never actually reached that
    stage before landing back at Final QC)."""
    code = FAIL_REASON_SOURCE_STAGE_CODE.get((failure_reason or "").strip())
    if not code:
        return None
    wo = (await db.execute(
        select(WorkOrder).where(
            WorkOrder.device_id == device_id, WorkOrder.stage == code,
            WorkOrder.assigned_username.isnot(None),
        ).order_by(WorkOrder.assigned_at.desc())
    )).scalars().first()
    if not wo:
        return None
    return {"user_id": wo.assigned_user_id, "username": wo.assigned_username, "name": wo.assigned_name}


async def _get_devices_at_stage(db: AsyncSession, stage: DeviceStage, entity: str = ""):
    where = [Device.current_stage == stage, Device.is_trashed == False]
    if entity:
        where.append(Device.entity == entity)
    else:
        # No explicit Entity filter -> exclude the External Partner test-data
        # entity by default, same convention as routers/dashboard.py,
        # devices.py, stock.py and entity_movement.py. NULL-safe: `entity !=
        # X` alone silently drops every entity-less device too, since SQL
        # NULL != X evaluates to NULL, not TRUE.
        where.append(or_(Device.entity.is_(None), Device.entity != EXTERNAL_PARTNER_TEST_ENTITY))
    result = await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(*where)
        .order_by(Device.updated_at.desc())
    )
    return result.all()


async def _passed_device_rows(db: AsyncSession) -> list[dict]:
    """Devices Passed tab — one row PER TAG, not grouped by bucket (2026-09-23
    redesign, mirrors _failed_device_rows below for the same reason: a single
    Bucket Name can hold tags decided at different times with different Pass
    Notes, so a bucket-level row can no longer show correct per-tag detail or
    be moved as one unit). Feeds the per-row "Move to Inventory" button, the
    multi-select "Bulk Move to Inventory" button, and the search box."""
    rows = (await db.execute(
        select(Device).where(
            Device.current_stage == DeviceStage.final_qc_pass_hold,
            Device.final_qc_status == "pass",
            Device.is_active == True,
            Device.is_trashed == False,
        ).order_by(Device.updated_at.desc())
    )).scalars().all()
    bucket_ids = {d.bucket_id for d in rows if d.bucket_id}
    buckets_by_id = {}
    if bucket_ids:
        b_rows = (await db.execute(select(Bucket).where(Bucket.id.in_(bucket_ids)))).scalars().all()
        buckets_by_id = {b.id: b for b in b_rows}
    out = []
    for d in rows:
        b = buckets_by_id.get(d.bucket_id)
        out.append({
            "device_id": str(d.id), "barcode": d.barcode,
            "bucket_name": (b.name or b.bucket_number) if b else None,
            "pass_notes": d.fqc_pass_notes,
        })
    return out


async def _failed_device_rows(db: AsyncSession) -> list[dict]:
    """Devices Failed tab (2026-08-29 redesign) — one row PER TAG, not grouped
    by bucket: since the bucket-lock removal, one Bucket Name can hold tags
    with different Failure Reasons and resolved engineers, so a bucket-level
    row can no longer show a single correct Engineer Name / Failure Reason
    or be moved as one unit. Feeds the per-row "Move" button, the
    multi-select "Bulk Move" button, and the Bucket Name/Tag Number search box."""
    rows = (await db.execute(
        select(Device).where(
            Device.current_stage == DeviceStage.final_qc_fail_hold,
            Device.final_qc_status == "fail",
            Device.is_active == True,
            Device.is_trashed == False,
        ).order_by(Device.updated_at.desc())
    )).scalars().all()
    bucket_ids = {d.bucket_id for d in rows if d.bucket_id}
    buckets_by_id = {}
    if bucket_ids:
        b_rows = (await db.execute(select(Bucket).where(Bucket.id.in_(bucket_ids)))).scalars().all()
        buckets_by_id = {b.id: b for b in b_rows}
    out = []
    for d in rows:
        b = buckets_by_id.get(d.bucket_id)
        reason = (d.fqc_failure_reason or "").strip()
        engineer_info = await _resolve_fail_engineer(db, d.id, reason)
        out.append({
            "device_id": str(d.id), "barcode": d.barcode,
            "bucket_name": (b.name or b.bucket_number) if b else None,
            "bucket_number": b.bucket_number if b else None,
            "failure_reason": reason or None,
            "engineer_name": engineer_info["name"] if engineer_info else None,
            "final_notes": d.fqc_final_notes,
        })
    return out


@router.get("", response_class=HTMLResponse)
async def cosmetic_dashboard(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    """Overview of all cosmetic pipeline stages."""
    stage_data = {}
    for stage in COSMETIC_PIPELINE:
        devices = await _get_devices_at_stage(db, stage)
        stage_data[stage] = {
            "label": STAGE_LABELS[stage],
            "devices": devices,
            "count": len(devices),
        }
    return templates.TemplateResponse("cosmetic/dashboard.html", {
        "request": request, "current_user": current_user,
        "stage_data": stage_data, "pipeline": COSMETIC_PIPELINE,
    })


@router.get("/all_tags", response_class=HTMLResponse)
async def cosmetic_all_tags(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    """Last tab on the Cosmetic & Paint hub — every tag currently anywhere in
    the 8-stage pipeline (Cosmetic Received through Cosmetic Completed) in
    one flat, read-only table: same shape as the Received table minus WorkID,
    with a Stage column standing in for the per-page action buttons. Must be
    registered ahead of the /{stage_name} route below, or "all_tags" would be
    parsed as a (nonexistent) DeviceStage and 404."""
    result = await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage.in_(COSMETIC_NAV_STAGES))
        .order_by(Device.updated_at.desc())
    )
    devices = result.all()
    device_ids = [d.id for d, _ in devices]

    # ── Most recent L1/L2 Engineer per device — same resolution as every
    # single-stage page (stage-agnostic already: WorkOrder.stage == "l1").
    l1l2_engineer_map: dict[str, str] = {}
    if device_ids:
        wo_rows = await db.execute(
            select(WorkOrder.device_id, WorkOrder.assigned_name, WorkOrder.assigned_at)
            .where(WorkOrder.device_id.in_(device_ids), WorkOrder.stage == "l1",
                   WorkOrder.assigned_name.isnot(None))
            .order_by(WorkOrder.assigned_at.desc())
        )
        for did, name, _ in wo_rows.all():
            l1l2_engineer_map.setdefault(str(did), name)

    # ── Assigned to / Assigned Date: the same "latest WorkOrder tagged with
    # this page's own MOVE_STAGE_CODE" resolution every single-stage page's
    # workid_map uses — done once across every stage code here since these
    # devices span all 8 stages, then picked per-device by ITS OWN current
    # stage's code (a device's history may carry WorkOrders from earlier
    # stages too; only the code matching where it sits right now applies).
    assigned_map: dict[str, dict] = {}
    if device_ids:
        wo_rows2 = await db.execute(
            select(WorkOrder.device_id, WorkOrder.stage, WorkOrder.assigned_name,
                   WorkOrder.assigned_username, WorkOrder.assigned_at)
            .where(WorkOrder.device_id.in_(device_ids), WorkOrder.stage.in_(list(MOVE_STAGE_CODE.values())))
            .order_by(WorkOrder.assigned_at.desc())
        )
        latest_by_key: dict = {}
        for did, wstage, name, uname, at in wo_rows2.all():
            latest_by_key.setdefault((did, wstage), {"name": name, "username": uname, "date": at})
        for d, _ in devices:
            code = MOVE_STAGE_CODE.get(d.current_stage)
            assigned_map[str(d.id)] = (latest_by_key.get((d.id, code)) or {}) if code else {}

    # ── Manager/Member visibility — Admin and Cosmetic Manager always see
    # every tag here (wired directly to role, not Group Config membership);
    # a single-stage cosmetic role (e.g. "Cosmetic Cleaning") never does,
    # even if they happen to manage an unrelated Group Config team. Every
    # other role falls back to the pre-existing Group Config rule.
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    is_stage_role = _is_cosmetic_stage_role(role_val)
    manager_mode = (role_val in ("admin", "cosmetic_manager")
                    or (not is_stage_role and await is_group_manager(db, current_user.username)))
    if not manager_mode:
        devices = [(d, ln) for d, ln in devices
                   if assigned_map.get(str(d.id), {}).get("username") == current_user.username]

    # ── Flow Data (below the main table) — Admin/Cosmetic Manager only.
    flow_rows, flow_user_options = [], {}
    if role_val in ("admin", "cosmetic_manager"):
        flow_rows = (await db.execute(
            select(CosmeticFlowRow).order_by(CosmeticFlowRow.created_at)
        )).scalars().all()
        all_active = (await db.execute(
            select(User).where(User.status == True).order_by(User.full_name)
        )).scalars().all()
        for field, _, module in FLOW_STAGE_COLUMNS:
            flow_user_options[field] = [
                {"id": str(u.id), "name": u.full_name or u.username}
                for u in all_active
                if has_perm(u.role.value if hasattr(u.role, "value") else str(u.role), module, "enable")
            ]

    # ── Flow badges (2026-10-19) — one per saved Flow Data row, after the
    # table's Page view / row count: how many tags in this pipeline are
    # currently assigned (Assigned to column) to any user mapped in that
    # flow's six stage cells. Same Admin/Cosmetic Manager gate as Flow Data
    # itself; those roles already see every tag, so this is the full total.
    # A user mapped in two flows counts their tags in both.
    flow_badges = []
    if flow_rows:
        username_by_id = {u.id: u.username for u in all_active}
        for idx, fr in enumerate(flow_rows, start=1):
            unames = {username_by_id.get(getattr(fr, f"{field}_user_id"))
                      for field, _, _ in FLOW_STAGE_COLUMNS}
            unames.discard(None)
            count = sum(1 for d, _ in devices
                        if assigned_map.get(str(d.id), {}).get("username") in unames)
            flow_badges.append({"label": fr.label or f"Flow {idx}", "count": count})

    return templates.TemplateResponse("cosmetic/all_tags.html", {
        "request": request, "current_user": current_user,
        "devices": devices, "now": app_now(),
        "parts_badges": await parts_badge_map(db, [d.id for d, *_ in devices]),
        "pipeline": COSMETIC_NAV_STAGES, "stage_labels": STAGE_LABELS,
        "l1l2_engineer_map": l1l2_engineer_map,
        "assigned_map": assigned_map, "manager_mode": manager_mode,
        "flow_columns": FLOW_STAGE_COLUMNS, "flow_rows": flow_rows,
        "flow_user_options": flow_user_options, "flow_badges": flow_badges,
    })


async def flow_data_allowed(current_user: User = Depends(get_current_user)) -> User:
    """Admin/Cosmetic Manager only, deliberately NOT require_roles(): that
    helper's custom-role backdoor lets any admin-created role through
    non-admin-only gates (they're normally governed by the Permission
    Matrix instead) — but Flow Data has no Permission Matrix module of its
    own, so a custom "Cosmetic Cleaning" role must not slip through it."""
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if role_val not in ("admin", "cosmetic_manager"):
        raise HTTPException(403, "Only Admin and Cosmetic Manager can edit Flow Data.")
    return current_user


@router.post("/flow-data/save")
async def cosmetic_flow_data_save(
    row_id: str = Form(""),
    label: str = Form(""),
    cleaning_user_id: str = Form(""),
    putty_user_id: str = Form(""),
    dry_sanding_user_id: str = Form(""),
    masking_user_id: str = Form(""),
    painting_user_id: str = Form(""),
    water_sanding_user_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(flow_data_allowed),
):
    """Create or update one Flow Data row. Cells are optional — a row can be
    saved with some stages still blank and filled in later."""
    raw_cells = {
        "cleaning_user_id": cleaning_user_id, "putty_user_id": putty_user_id,
        "dry_sanding_user_id": dry_sanding_user_id, "masking_user_id": masking_user_id,
        "painting_user_id": painting_user_id, "water_sanding_user_id": water_sanding_user_id,
    }
    cell_values = {
        field: (uuid_module.UUID(raw.strip()) if raw and raw.strip() else None)
        for field, raw in raw_cells.items()
    }

    if row_id:
        row = (await db.execute(
            select(CosmeticFlowRow).where(CosmeticFlowRow.id == uuid_module.UUID(row_id))
        )).scalar_one_or_none()
        if not row:
            raise HTTPException(404, "Flow row not found.")
        row.updated_by = current_user.username
    else:
        row = CosmeticFlowRow(created_by=current_user.username)
        db.add(row)

    row.label = label.strip() or None
    for field, value in cell_values.items():
        setattr(row, field, value)

    await db.commit()
    await db.refresh(row)
    return JSONResponse({"ok": True, "id": str(row.id)})


@router.post("/flow-data/delete")
async def cosmetic_flow_data_delete(
    row_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(flow_data_allowed),
):
    row = (await db.execute(
        select(CosmeticFlowRow).where(CosmeticFlowRow.id == uuid_module.UUID(row_id))
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Flow row not found.")
    await db.delete(row)
    await db.commit()
    return JSONResponse({"ok": True})


@router.get("/{stage_name}", response_class=HTMLResponse)
async def cosmetic_stage_list(stage_name: str, request: Request, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed),
                              entity: str = Query(default="")):
    try:
        stage = DeviceStage(stage_name)
    except ValueError:
        raise HTTPException(404)
    if stage not in COSMETIC_PIPELINE:
        raise HTTPException(404)
    devices = await _get_devices_at_stage(db, stage, entity=entity)
    next_stage = NEXT_COSMETIC.get(stage)

    # Final QC (#18): show IQC data + post-repair data read-only, then grade + Ready to Sale
    if stage == DeviceStage.final_qc:
        device_ids = [d.id for d, _ in devices]
        iqc_map, repairs_map = {}, {}
        if device_ids:
            iqcs = (await db.execute(
                select(IQCInspection).where(IQCInspection.device_id.in_(device_ids))
            )).scalars().all()
            for i in iqcs:
                iqc_map[str(i.device_id)] = i
            rjs = (await db.execute(
                select(RepairJob).where(RepairJob.device_id.in_(device_ids))
                .order_by(RepairJob.started_at.desc())
            )).scalars().all()
            for r in rjs:
                repairs_map.setdefault(str(r.device_id), []).append(r)
        # ── Parts consumed: required parts (from IQC) + whether the part request
        #    was Received → "Changed", else "Not Changed". ─────────────────────
        parts_map = {}
        if device_ids:
            prs = (await db.execute(
                select(PartRequest).where(PartRequest.device_id.in_(device_ids))
                .order_by(PartRequest.created_at.desc())
            )).scalars().all()
            pr_by_dev_part = {}
            for r in prs:
                pr_by_dev_part.setdefault(str(r.device_id), {}).setdefault(r.part_name, r)
            for d, _ in devices:
                rows = compute_required(iqc_map.get(str(d.id)), d)
                plist = []
                for row in rows:
                    if row.get("required"):
                        req = pr_by_dev_part.get(str(d.id), {}).get(row["label"])
                        plist.append({"label": row["label"],
                                      "changed": bool(req and req.status == "received")})
                parts_map[str(d.id)] = plist

            # ── Changed parts' pricing (Part Master unit price × Device Detail's
            #    verified/handed-over qty) — feeds into the Pricing section below.
            changed_reqs = [r for r in prs if r.status == "received" and r.part_id]
            changed_part_ids = {r.part_id for r in changed_reqs}
            spare_parts_by_id = {}
            if changed_part_ids:
                sp_rows = (await db.execute(
                    select(SparePart).where(SparePart.id.in_(changed_part_ids))
                )).scalars().all()
                spare_parts_by_id = {sp.id: sp for sp in sp_rows}
            changed_parts_pricing = {}
            for r in changed_reqs:
                sp = spare_parts_by_id.get(r.part_id)
                if not sp:
                    continue
                qty = r.qty_handed_over or 0
                unit_price = float(sp.unit_price or 0)
                changed_parts_pricing.setdefault(str(r.device_id), []).append({
                    "label": r.part_name, "unit_price": unit_price,
                    "qty": qty, "total": unit_price * qty,
                })

        # ── Pricing: current unit price → after-repair price (parts actually
        #    consumed, real cost) → updated price shown to the user. ─────────
        price_map = {}
        if device_ids:
            cost_rows = (await db.execute(
                select(
                    SparePartConsumption.device_id,
                    func.coalesce(func.sum(SparePartConsumption.total_cost), 0).label("parts_cost"),
                )
                .where(SparePartConsumption.device_id.in_(device_ids))
                .group_by(SparePartConsumption.device_id)
            )).all()
            parts_cost_by_device = {str(r.device_id): float(r.parts_cost or 0) for r in cost_rows}

            # Avg Lot Price — fallback for Current Unit Price when a device
            # has no device_price of its own (blank/None). Same lot.buying_price
            # / lot.qty computation IQC intake already uses to SEED device_price
            # in the first place (routers/iqc.py) — devices that never got that
            # seed, or had it cleared, would otherwise show a bare ₹0.00.
            lot_avg_by_device = {}
            lot_rows = (await db.execute(
                select(Device.id, Lot.buying_price, Lot.qty)
                .join(Lot, Device.lot_id == Lot.id)
                .where(Device.id.in_(device_ids))
            )).all()
            for did, buying_price, qty in lot_rows:
                lot_avg_by_device[str(did)] = float(buying_price / qty) if qty else 0.0

            # Labour Price — Admin -> Cost Config's Labour Rate
            # (repair_labour_rate), the same CostConfig row routers/dashboard.py
            # and routers/stock.py already read for Lot P&L. Informational
            # only — not folded into After Repair Price / Updated Price, which
            # stay parts-cost-only as before. (The "Other Price" row that used
            # to sit beside it was removed 2026-10-07; Other Rates now feed the
            # Ready to Sale Min Selling Price instead — services/ready_pricing.py.)
            _cfg_rows = (await db.execute(select(CostConfig))).scalars().all()
            _cfg = {r.key: float(r.value) for r in _cfg_rows}
            labour_rate = _cfg.get("repair_labour_rate", 150.0)

            for d, _ in devices:
                if d.device_price is not None:
                    current_price = float(d.device_price)
                    current_price_is_avg_lot = False
                else:
                    current_price = lot_avg_by_device.get(str(d.id), 0.0)
                    current_price_is_avg_lot = True
                parts_cost = parts_cost_by_device.get(str(d.id), 0.0)
                changed_cost = sum(p["total"] for p in changed_parts_pricing.get(str(d.id), []))
                after_repair_price = current_price + parts_cost + changed_cost
                price_map[str(d.id)] = {
                    "current_price": current_price,
                    "current_price_is_avg_lot": current_price_is_avg_lot,
                    "parts_cost": parts_cost,
                    "changed_parts": changed_parts_pricing.get(str(d.id), []),
                    "changed_parts_cost": changed_cost,
                    "after_repair_price": after_repair_price,
                    "updated_price": after_repair_price,
                    "labour_price": labour_rate,
                }

        bucket_ids_for_page = {d.bucket_id for d, _ in devices if d.bucket_id}
        bucket_name_map = {}
        if bucket_ids_for_page:
            b_rows = (await db.execute(select(Bucket).where(Bucket.id.in_(bucket_ids_for_page)))).scalars().all()
            bname_by_id = {b.id: b.name for b in b_rows}
            # Pre-fills the Bucket Name text box on the Final QC Decision tab
            # for a device that already carries a bucket_id — "" (not a dash)
            # since this feeds a form value, not read-only display text.
            bucket_name_map = {str(d.id): bname_by_id.get(d.bucket_id, "") for d, _ in devices if d.bucket_id}

        # ── Latest Stress/QC report per device, for the "Stress/QC Report"
        # download button next to the Tag Number in the right panel (same
        # latest-row rule as the Stress Test page, routers/qc.py). ─────────
        stress_map: dict[str, dict] = {}
        _bcs = [d.barcode for d, _ in devices if d.barcode]
        if _bcs:
            for bc, rid, ov in (await db.execute(
                select(StressTestResult.barcode, StressTestResult.id, StressTestResult.overall_status)
                .where(StressTestResult.barcode.in_(_bcs))
                .order_by(StressTestResult.barcode, StressTestResult.id.desc())
                .distinct(StressTestResult.barcode)
            )).all():
                stress_map[bc] = {"result_id": rid, "overall_status": ov or ""}

        passed_devices = await _passed_device_rows(db)
        failed_devices = await _failed_device_rows(db)

        # ── Most recent L1/L2 Engineer per device — shown next to Lot Number
        # in each device's header card so whoever's deciding Fail can see who
        # last worked the hardware, before picking a destination in the
        # Assign Bucket modal later. Same resolution as every other page's
        # L1/L2 Engineer column (WorkOrder.stage == "l1", latest wins). ──────
        l1l2_engineer_map: dict[str, str] = {}
        if device_ids:
            l1l2_rows = await db.execute(
                select(WorkOrder.device_id, WorkOrder.assigned_name, WorkOrder.assigned_at)
                .where(WorkOrder.device_id.in_(device_ids), WorkOrder.stage == "l1",
                       WorkOrder.assigned_name.isnot(None))
                .order_by(WorkOrder.assigned_at.desc())
            )
            for did, name, _ in l1l2_rows.all():
                l1l2_engineer_map.setdefault(str(did), name)

        return templates.TemplateResponse("cosmetic/final_qc.html", {
            "request": request, "current_user": current_user,
            "stage": stage, "stage_label": STAGE_LABELS[stage],
            "devices": devices, "iqc_map": iqc_map, "repairs_map": repairs_map,
            "parts_map": parts_map, "price_map": price_map,
            "changed_parts_map": await changed_parts_by_device(db, device_ids),
            "parts_badges": await parts_badge_map(db, device_ids),
            "l1l2_engineer_map": l1l2_engineer_map,
            "pipeline": COSMETIC_NAV_STAGES, "stage_labels": STAGE_LABELS,
            "bucket_name_map": bucket_name_map, "stress_map": stress_map,
            "passed_devices": passed_devices, "failed_devices": failed_devices,
            "entity_options": await entity_values(db), "f_entity": entity,
        })

    # ── Most recent L1/L2 Engineer per device, for the "L1/L2 Engineer" column
    # and to pre-populate the Fail modal's context — same resolution as the
    # Stress Test page (routers/qc.py qc_list) and routers/repair.py's own
    # "assigned to me" queries: latest WorkOrder at stage="l1" wins.
    device_ids = [d.id for d, _ in devices]
    l1l2_engineer_map: dict[str, str] = {}
    if device_ids:
        wo_rows = await db.execute(
            select(WorkOrder.device_id, WorkOrder.assigned_name, WorkOrder.assigned_at)
            .where(WorkOrder.device_id.in_(device_ids), WorkOrder.stage == "l1",
                   WorkOrder.assigned_name.isnot(None))
            .order_by(WorkOrder.assigned_at.desc())
        )
        for did, name, _ in wo_rows.all():
            l1l2_engineer_map.setdefault(str(did), name)

    # ── L1/L2 engineer pool for the Fail modal's dropdown — identical pool to
    # the Stress Test page's own Fail modal (routers/qc.py qc_list).
    l1l2_result = await db.execute(
        select(User).where(
            User.role.in_([UserRole.l1_engineer, UserRole.l2_engineer]),
            User.status == True,
        ).order_by(User.full_name)
    )
    l1l2_engineers = [
        {"id": str(u.id), "name": u.full_name or u.username, "role": u.role.value}
        for u in l1l2_result.scalars().all()
    ]

    # ── WorkID column (every page in this batch): the latest WorkOrder tagged
    # with THIS page's own MOVE_STAGE_CODE — created either by the previous
    # stage's Move modal, or (for Cosmetic Received specifically) by the
    # Stress Test "Complete" button. Also doubles as "Assigned to"/"Assigned
    # Date" on the Received/Completed templates. ───────────────────────────
    workid_map: dict[str, dict] = {}
    stage_code = MOVE_STAGE_CODE.get(stage)
    if device_ids and stage_code:
        wo_rows2 = await db.execute(
            select(WorkOrder.device_id, WorkOrder.work_id, WorkOrder.assigned_name,
                   WorkOrder.assigned_username, WorkOrder.assigned_at)
            .where(WorkOrder.device_id.in_(device_ids), WorkOrder.stage == stage_code)
            .order_by(WorkOrder.assigned_at.desc())
        )
        for did, wid, name, uname, at in wo_rows2.all():
            workid_map.setdefault(str(did), {
                "work_id": wid, "name": name, "username": uname, "date": at,
            })

    # ── Manager/Member visibility: Admin and Cosmetic Manager always see
    # every tag on this page and the stage nav tabs (wired directly to role,
    # not Group Config membership); a single-stage cosmetic role never does,
    # even if they happen to manage an unrelated Group Config team. Every
    # other role falls back to the pre-existing Group Config rule. ─────────
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    is_stage_role = _is_cosmetic_stage_role(role_val)
    manager_mode = (role_val in ("admin", "cosmetic_manager")
                    or (not is_stage_role and await is_group_manager(db, current_user.username)))
    if not manager_mode:
        devices = [(d, ln) for d, ln in devices
                   if workid_map.get(str(d.id), {}).get("username") == current_user.username]

    move_pool = await _move_assignee_pool(db, current_user)

    if stage in (DeviceStage.cosmetic_received, DeviceStage.cosmetic_completed):
        template_name = ("cosmetic/received.html" if stage == DeviceStage.cosmetic_received
                          else "cosmetic/completed.html")
        return templates.TemplateResponse(template_name, {
            "request": request, "current_user": current_user,
            "stage": stage, "stage_label": STAGE_LABELS[stage],
            "devices": devices,
            "next_stage": next_stage,
            "next_stage_label": STAGE_LABELS.get(next_stage, "Ready to Sale") if next_stage else "Ready to Sale",
            "pipeline": COSMETIC_NAV_STAGES, "stage_labels": STAGE_LABELS,
            "l1l2_engineer_map": l1l2_engineer_map, "l1l2_engineers": l1l2_engineers,
            "workid_map": workid_map, "now": app_now(),
            "manager_mode": manager_mode, "move_pool": move_pool,
            "parts_badges": await parts_badge_map(db, [d.id for d, *_ in devices]),
        })

    return templates.TemplateResponse("cosmetic/stage.html", {
        "request": request, "current_user": current_user,
        "stage": stage, "stage_label": STAGE_LABELS[stage],
        "devices": devices,
        "next_stage": next_stage,
        "next_stage_label": STAGE_LABELS.get(next_stage, "Ready to Sale") if next_stage else "Ready to Sale",
        "pipeline": COSMETIC_NAV_STAGES, "stage_labels": STAGE_LABELS,
        "l1l2_engineer_map": l1l2_engineer_map, "l1l2_engineers": l1l2_engineers,
        "workid_map": workid_map, "manager_mode": manager_mode, "move_pool": move_pool,
        "parts_badges": await parts_badge_map(db, [d.id for d, *_ in devices]),
    })


@router.post("/advance")
async def advance_stage(
    barcode: str = Form(...),
    notes: str = Form(""),
    target: str = Form(""),
    engineer_user_id: str = Form(""),
    final_qc_status: str = Form("pass"),
    failure_reason: str = Form(""),
    pass_notes: str = Form(""),
    bucket_name: str = Form(""),
    grade: str = Form(""),
    warehouse: str = Form(""),
    updated_make: str = Form(""),
    updated_model: str = Form(""),
    updated_cpu: str = Form(""),
    updated_generation: str = Form(""),
    updated_ram: str = Form(""),
    updated_hdd: str = Form(""),
    touch_type: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Move a device to the next cosmetic stage."""
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")

    current = device.current_stage
    next_stage = NEXT_COSMETIC.get(current)
    if not next_stage:
        raise HTTPException(400, f"Device {barcode} is not in a cosmetic pipeline stage")

    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)

    # "Skip Cosmetic" — jump straight to Final QC from any cosmetic stage
    if target == "final_qc" and current in COSMETIC_PIPELINE and current != DeviceStage.final_qc:
        next_stage = DeviceStage.final_qc

    # Final QC: apply spec corrections + handle fail
    if current == DeviceStage.final_qc:
        if not has_perm(role_val, "cosmetic_finalqc", "edit"):
            raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the cosmetic_finalqc module.")

        # ── Auto-attribution (2026-09-01, replaces the old "Pick This"
        # button): whoever submits this Final QC decision automatically
        # gets a completed "fqc" WorkID for it, visible on /workid-status
        # like any other WorkID — no separate claim-then-decide step. This
        # also retires the class of bug where a tag that left Final QC
        # through some path other than this one (leaving a stale "pending"
        # pick behind) would come back still showing "Picked by <name>" for
        # everyone, with no way to clear it short of that same exit path.
        # Any leftover pending pick from the old flow is still closed out
        # here for cleanliness. ──────────────────────────────────────────
        await db.execute(
            update(WorkOrder)
            .where(WorkOrder.device_id == device.id, WorkOrder.stage == "fqc",
                   WorkOrder.status == "pending")
            .values(status="completed", completed_at=app_now())
        )
        fqc_work_id = await _gen_work_id(db)
        db.add(WorkOrder(
            work_id=fqc_work_id, device_id=device.id, barcode=device.barcode,
            stage=MOVE_STAGE_CODE[DeviceStage.final_qc], assigned_role=role_val,
            assigned_user_id=current_user.id, assigned_username=current_user.username,
            assigned_name=current_user.full_name, status="completed",
            completed_at=app_now(), created_by=current_user.username,
        ))

        # Bucket is a free-text Bucket Name here, not a dropdown of existing
        # buckets — typing a name that doesn't exist yet creates it. Before
        # attaching the current device, every OTHER device already linked to
        # that bucket gets its bucket_id cleared UNLESS it is itself sitting
        # at Final QC Pass/Fail Hold — those are left alone so several tags
        # decided into the same Bucket Name keep accumulating together.
        # Anything else sharing the bucket_id (long since sold, scrapped, or
        # mid-repair from an earlier, unrelated use of the same name) is what
        # a later "move bucket to Production" would otherwise sweep in.
        bucket_name = bucket_name.strip()
        if bucket_name:
            bucket = (await db.execute(
                select(Bucket).where(func.lower(Bucket.name) == bucket_name.lower())
            )).scalars().first()
            if not bucket:
                bucket = Bucket(name=bucket_name, bucket_number=_new_bucket_number(),
                                 created_by=current_user.username)
                db.add(bucket)
                await db.flush()
            await db.execute(
                update(Device)
                .where(Device.bucket_id == bucket.id,
                       Device.id != device.id,
                       Device.current_stage.notin_(
                           [DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold]))
                .values(bucket_id=None)
            )
            device.bucket_id = bucket.id
        else:
            device.bucket_id = None
        if updated_make: device.brand = updated_make
        if updated_model: device.model = updated_model
        if updated_cpu: device.cpu = updated_cpu
        if updated_generation: device.generation = updated_generation
        if warehouse: device.warehouse = warehouse
        # Touch Type radio (Touch / Non-Touch) — saved on both Pass and Fail.
        # Only a recognised value overwrites; anything else leaves it as-is.
        _tt = (touch_type or "").strip().lower()
        if _tt in ("touch", "non_touch"):
            device.touch_type = _tt
        # Case-insensitive: master data stores "Fail"/"Pass" (title case), so an exact
        # == "fail" match silently treats every Fail as a Pass in production.
        _is_fail = final_qc_status.strip().lower() == "fail"
        device.final_qc_status = "fail" if _is_fail else "pass"
        if _is_fail:
            resolved_reason = (failure_reason or "").strip() or None

            # Multiple tags can freely share one Bucket Name regardless of
            # reason/engineer (2026-08 — dropped the earlier "must match" lock
            # so any combination can be added to the same bucket). This just
            # tracks the most recently resolved reason/engineer as FYI info
            # for the "Engineer Name" column on Devices Failed — the actual
            # hand-off is now a manual pick via the Assign Bucket modal (see
            # /buckets/{bucket_id}/assign, same modal Production Manager uses).
            if device.bucket_id:
                engineer_info = await _resolve_fail_engineer(db, device.id, resolved_reason)
                bkt = (await db.execute(
                    select(Bucket).where(Bucket.id == device.bucket_id)
                )).scalar_one_or_none()
                if bkt:
                    bkt.fail_reason = resolved_reason
                    if engineer_info:
                        bkt.fail_engineer_user_id = engineer_info["user_id"]
                        bkt.fail_engineer_username = engineer_info["username"]
                        bkt.fail_engineer_name = engineer_info["name"]

            # Hold here instead of auto-advancing to Cleaning — the device now
            # sits in the "Devices Failed" table until a human clicks "Move"
            # (or "Bulk Move") — see fqc_move_failed below.
            device.fqc_failure_reason = resolved_reason
            device.fqc_final_notes = (notes or "").strip() or None
            device.current_stage = DeviceStage.final_qc_fail_hold
            device.updated_at = app_now()
            movement = StageMovement(
                device_id=device.id, from_stage=current, to_stage=DeviceStage.final_qc_fail_hold,
                moved_by=current_user.username,
                notes=f"Final QC Failed — {failure_reason or 'Rework'}. {notes}"
            )
            db.add(movement)
            await db.commit()
            return RedirectResponse(
                url="/cosmetic/final_qc?warning=Final+QC+Failed+for+" + barcode + "+%E2%80%94+recorded+successfully",
                status_code=302
            )

        # Pass only — the Grade dropdown is hidden on fail but still posts its value.
        if grade: device.grade = grade

        # Verified RAM / Hard Drive part changes since the previous pass are
        # written onto the device's own spec now (services/parts_changed.py)
        # — before the pass StageMovement below, which is what marks them done.
        await apply_spec_changes_on_pass(db, device, current_user)

        # Pricing gate: on Final QC pass, finalize the device price as
        # acquisition price + consumed parts + changed (received) parts —
        # the same arithmetic the Final QC page's Pricing panel displays.
        # Run at most once per device (a rework loop that passes Final QC a
        # second time must not add the same parts costs again).
        already_priced = (await db.execute(
            select(StageMovement.id).where(
                StageMovement.device_id == device.id,
                StageMovement.notes.like("Final QC Passed — price finalized%"),
            ).limit(1)
        )).scalar_one_or_none()
        if not already_priced:
            try:
                parts_cost = float((await db.execute(
                    select(func.coalesce(func.sum(SparePartConsumption.total_cost), 0))
                    .where(SparePartConsumption.device_id == device.id)
                )).scalar() or 0)
                changed_cost = 0.0
                _prs = (await db.execute(
                    select(PartRequest).where(PartRequest.device_id == device.id,
                                              PartRequest.status == "received",
                                              PartRequest.part_id.isnot(None))
                )).scalars().all()
                _pids = {r.part_id for r in _prs}
                if _pids:
                    _sps = {sp.id: sp for sp in (await db.execute(
                        select(SparePart).where(SparePart.id.in_(_pids))
                    )).scalars().all()}
                    for r in _prs:
                        sp = _sps.get(r.part_id)
                        if sp:
                            changed_cost += float(sp.unit_price or 0) * (r.qty_handed_over or 0)
                base_price = float(device.device_price or 0)
                device.device_price = base_price + parts_cost + changed_cost
                db.add(StageMovement(
                    device_id=device.id, from_stage=current, to_stage=current,
                    moved_by=current_user.username,
                    notes=(f"Final QC Passed — price finalized: ₹{base_price:,.0f} + "
                           f"parts ₹{parts_cost:,.0f} + changed ₹{changed_cost:,.0f} "
                           f"= ₹{float(device.device_price):,.0f}")))
            except Exception:
                pass  # pricing snapshot must never block the QC pass itself

        # Hold here instead of auto-advancing to Ready to Sale — the device
        # now sits in the "Devices Passed" table until a human clicks
        # "Move to Inventory" (see /cosmetic/final-qc/move-to-inventory).
        device.fqc_pass_notes = (pass_notes or "").strip() or None
        device.current_stage = DeviceStage.final_qc_pass_hold
        device.updated_at = app_now()
        db.add(StageMovement(
            device_id=device.id, from_stage=current, to_stage=DeviceStage.final_qc_pass_hold,
            moved_by=current_user.username,
            notes=f"Final QC Passed — {pass_notes or ''}".strip(),
        ))
        await db.commit()
        return RedirectResponse(
            url="/cosmetic/final_qc?success=Final+QC+Passed+for+" + barcode,
            status_code=302
        )

    prev = current
    # ── Close out the ORIGIN stage's own pending WorkOrder (whoever was
    # assigned to work this device at `prev`) so it gets a Completed Date on
    # /workid-status — mirrors the closure the Final QC branch above already
    # does for its own "fqc" WorkOrder. Without this, a mid-pipeline stage's
    # WorkOrder stayed "pending" forever with no completion time even long
    # after the tag moved on, so "date completed Cleaning/Putty/…" had
    # nothing to show. Covers every cosmetic-pipeline stage uniformly since
    # Final QC's own move never reaches this generic code (it returns
    # earlier, inside the `if current == DeviceStage.final_qc:` branch). ───
    prev_stage_code = MOVE_STAGE_CODE.get(prev)
    if prev_stage_code:
        await db.execute(
            update(WorkOrder)
            .where(WorkOrder.device_id == device.id, WorkOrder.stage == prev_stage_code,
                   WorkOrder.status == "pending")
            .values(status="completed", completed_at=app_now())
        )

    # A device arriving at Final QC releases any bucket it still carries from
    # an earlier stage (e.g. a Stock In bucket never explicitly released) — a
    # stale bucket_id here is how an unrelated old tag sharing that reused
    # bucket number ended up swept into a later bulk bucket action against it.
    # Final QC's own Bucket dropdown starts every arriving device clean.
    if next_stage == DeviceStage.final_qc:
        device.bucket_id = None

    # ── Permission check applies on all 8 cosmetic-line pages regardless of
    # whether this particular Move needs an assignee. ──────────────────────
    if current in PERM_MODULE_BY_STAGE:
        module_key = PERM_MODULE_BY_STAGE[current]
        if not has_perm(role_val, module_key, "edit"):
            raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the {module_key} module.")

    # ── Assignment required for every Move EXCEPT one that lands on Final QC
    # or Cosmetic Completed (Cosmetic Completed's normal "Move to Final QC",
    # Cosmetic Received's "skip cosmetic stages" button, and Water Sanding's
    # "Move to Cosmetic Completed" — Cosmetic Completed is a holding stage
    # the Cosmetic Manager role handles broadly, not a per-tag hand-off):
    # pick who owns the device at its new stage and issue a fresh WorkID for
    # it — shows as that stage's WorkID column and on /workid-status. Final
    # QC's own Pass/Fail decisioning (handled above) never reaches here. ───
    assigned_engineer = None
    if current in ASSIGN_ON_MOVE_STAGES and next_stage not in (DeviceStage.final_qc, DeviceStage.cosmetic_completed):
        if not engineer_user_id:
            # Flow Data auto-assign (All Tags -> Flow Data): if the mover
            # sits in a saved flow row for this exact stage transition, skip
            # the manual pick entirely — no modal on the frontend. Falls
            # through to the same "select a user" error below when no
            # usable flow match exists, which is exactly what makes the
            # frontend fall back to the pre-existing modal.
            assigned_engineer = await _resolve_flow_next_user(db, current, next_stage, current_user.id)
            if not assigned_engineer:
                raise HTTPException(400, "Select a user to assign this device to before moving it.")
        else:
            try:
                eng_uuid = uuid_module.UUID(engineer_user_id)
            except (ValueError, AttributeError, TypeError):
                raise HTTPException(400, "Invalid user selected")
            assigned_engineer = (await db.execute(
                select(User).where(User.id == eng_uuid, User.status == True)
            )).scalar_one_or_none()
            if not assigned_engineer:
                raise HTTPException(400, "Selected user is not an active user")

    device.current_stage = next_stage
    device.updated_at = app_now()
    movement = StageMovement(
        device_id=device.id, from_stage=prev, to_stage=next_stage,
        moved_by=current_user.username,
        notes=notes or f"Advanced from {STAGE_LABELS.get(prev, prev.value)} to {STAGE_LABELS.get(next_stage, next_stage.value)}"
    )
    db.add(movement)

    work_id = None
    if assigned_engineer:
        work_id = await _gen_work_id(db)
        eng_role_val = (assigned_engineer.role.value if hasattr(assigned_engineer.role, "value")
                       else str(assigned_engineer.role))
        db.add(WorkOrder(
            work_id=work_id, device_id=device.id, barcode=device.barcode,
            stage=MOVE_STAGE_CODE.get(next_stage, next_stage.value[:5]),
            assigned_role=eng_role_val, assigned_user_id=assigned_engineer.id,
            assigned_username=assigned_engineer.username, assigned_name=assigned_engineer.full_name,
            status="pending", created_by=current_user.username,
        ))
        await create_notification(
            db, user_id=assigned_engineer.id, title="Device Assigned to You",
            message=(f"{device.barcode} moved to {STAGE_LABELS.get(next_stage, next_stage.value)} "
                     f"and has been assigned to you (WorkID: {work_id})."),
            notification_type="info",
            barcode=device.barcode, brand=device.brand, model=device.model,
            stage=next_stage.value,
        )

    await db.commit()

    # The 8 cosmetic-line pages drive this via fetch() and stay on the same
    # page (reload just re-fetches this stage's now-updated table) — no
    # redirect to the destination stage's page. Deliberately keyed on
    # PERM_MODULE_BY_STAGE (all 8), not the narrower ASSIGN_ON_MOVE_STAGES —
    # Cosmetic Completed's un-modal'd Move still stays on the same page.
    if current in PERM_MODULE_BY_STAGE:
        return JSONResponse({"ok": True, "work_id": work_id, "moved_to": next_stage.value})

    if next_stage == DeviceStage.ready_to_sale:
        return RedirectResponse(url="/sales/ready?success=Device+moved+to+Ready+to+Sale", status_code=302)

    stage_name = next_stage.value
    return RedirectResponse(url=f"/cosmetic/{stage_name}?success=Device+{barcode}+moved+to+{stage_name.replace('_', '+')}", status_code=302)


@router.post("/final-qc/move-failed")
async def fqc_move_failed(
    barcodes: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Devices Failed tab's per-row "Move" and multi-select "Bulk Move" both
    post here — Move sends one barcode, Bulk Move sends the checked ones
    comma-joined. Each tag's OWN Failure Reason picks its destination
    (FAIL_REASON_TO_DEPT: Hardware -> L1/L2 Repair, Software -> Stress Test,
    Cosmetic -> Cosmetic Repair):

     - Engineer Name resolved (same lookup that fills the table's column,
       _resolve_fail_engineer) -> routes straight to that person, no modal.
     - Hardware + blank Engineer Name -> no L1/L2 engineer to hand it to, so
       instead of moving it into a stage with no owner it's parked in
       Production Manager's Tag Number Allocation queue
       (DeviceStage.trc_production) for manual allocation.
     - Software or Cosmetic -> always moves to Stress Test / Cosmetic
       Received respectively, whether or not an engineer resolved — those
       stages' own queues are where the tag gets picked up either way, so
       there's no "no owner" case to park like Hardware/L1/L2 has.

    A tag no longer sitting in Fail Hold, or with no recognized Failure
    Reason, is skipped and reported back rather than failing the whole batch."""
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if not has_perm(role_val, "cosmetic_finalqc", "edit"):
        raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the cosmetic_finalqc module.")

    codes = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not codes:
        raise HTTPException(400, "No tags selected.")

    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(codes))
    )).scalars().all()
    by_barcode = {d.barcode: d for d in devices}

    moved, skipped = [], []
    for code in codes:
        device = by_barcode.get(code)
        if not device or device.current_stage != DeviceStage.final_qc_fail_hold:
            skipped.append({"barcode": code, "reason": "No longer awaiting Final QC Fail routing."})
            continue
        reason = (device.fqc_failure_reason or "").strip()
        department = FAIL_REASON_TO_DEPT.get(reason)
        if not department:
            skipped.append({"barcode": code, "reason": "No recognized Failure Reason to route by."})
            continue

        engineer_info = await _resolve_fail_engineer(db, device.id, reason)
        engineer = None
        if engineer_info and engineer_info.get("user_id"):
            engineer = (await db.execute(
                select(User).where(User.id == engineer_info["user_id"], User.status == True)
            )).scalar_one_or_none()

        if not engineer and reason == "Hardware":
            # Hardware only: nobody resolved to hand it to — park it in
            # Production Manager's Tag Number Allocation queue instead of
            # moving it to L1/L2 Repair with no owner.
            device.current_stage = DeviceStage.trc_production
            device.updated_at = app_now()
            db.add(StageMovement(
                device_id=device.id, from_stage=DeviceStage.final_qc_fail_hold,
                to_stage=DeviceStage.trc_production, moved_by=current_user.username,
                notes=f"Final QC Fail — {reason}: no engineer resolved, sent to Tag Number Allocation",
            ))
            moved.append(code)
            continue

        moved_ok = await _apply_department_move(db, device, department, engineer, current_user)
        if moved_ok:
            moved.append(code)
        else:
            skipped.append({"barcode": code, "reason": "Could not resolve a destination stage."})

    if moved:
        await db.commit()
    return JSONResponse({"ok": True, "moved": moved, "skipped": skipped})


@router.post("/final-qc/move-passed")
async def fqc_move_passed(
    barcodes: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Devices Passed tab's per-row "Move to Inventory" and multi-select
    "Bulk Move to Inventory" both post here — same barcode-list shape as
    fqc_move_failed above. Each tag: final_qc_pass_hold -> ready_to_sale,
    identical transition to the older bucket-based fqc_move_to_inventory."""
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if not has_perm(role_val, "cosmetic_finalqc", "edit"):
        raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the cosmetic_finalqc module.")

    codes = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not codes:
        raise HTTPException(400, "No tags selected.")

    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(codes))
    )).scalars().all()
    by_barcode = {d.barcode: d for d in devices}

    moved, skipped = [], []
    for code in codes:
        device = by_barcode.get(code)
        if not device or device.current_stage != DeviceStage.final_qc_pass_hold:
            skipped.append({"barcode": code, "reason": "No longer awaiting Final QC Pass Hold."})
            continue
        device.current_stage = DeviceStage.ready_to_sale
        device.updated_at = app_now()
        db.add(StageMovement(
            device_id=device.id, from_stage=DeviceStage.final_qc_pass_hold, to_stage=DeviceStage.ready_to_sale,
            moved_by=current_user.username, notes="Moved to Inventory from Final QC Pass",
        ))
        await ensure_stage_location(db, device, DeviceStage.ready_to_sale, current_user)
        moved.append(code)

    if moved:
        await db.commit()
    return JSONResponse({"ok": True, "moved": moved, "skipped": skipped})


@router.post("/final-qc/edit/{barcode}")
async def fqc_edit_revert(
    barcode: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Devices Passed / Devices Failed tables' Edit button — sends a tag back
    to the main pending Final QC list so its decision can be redone. Reverses
    advance_stage()'s Pass/Fail branches above: clears the recorded decision
    and moves the device back to DeviceStage.final_qc, which alone drops it
    out of both tables (their queries filter on current_stage)."""
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if not has_perm(role_val, "cosmetic_finalqc", "edit"):
        raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the cosmetic_finalqc module.")

    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, "Device not found")
    if device.current_stage not in (DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold):
        raise HTTPException(409, "Tag is no longer awaiting a Final QC Pass/Fail decision.")

    prev = device.current_stage
    device.final_qc_status = None
    device.fqc_failure_reason = None
    device.fqc_final_notes = None
    device.fqc_pass_notes = None
    device.current_stage = DeviceStage.final_qc
    device.updated_at = app_now()
    db.add(StageMovement(
        device_id=device.id, from_stage=prev, to_stage=DeviceStage.final_qc,
        moved_by=current_user.username, notes="Final QC decision reverted via Edit — back to pending decision",
    ))
    await db.commit()
    return RedirectResponse(url=f"/cosmetic/final_qc?success={barcode}+returned+to+Final+QC", status_code=302)


@router.post("/send-to-cosmetic")
async def send_to_cosmetic(
    barcode: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("cosmetic_received", "add")),
):
    """Send a device from QC Check to the Cosmetic Received stage to begin cosmetic refurb."""
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")

    prev = device.current_stage
    device.current_stage = DeviceStage.cosmetic_received
    device.updated_at = app_now()
    movement = StageMovement(
        device_id=device.id, from_stage=prev, to_stage=DeviceStage.cosmetic_received,
        moved_by=current_user.username,
        notes=notes or "Sent to Cosmetic Refurbishment"
    )
    db.add(movement)
    await db.commit()
    return RedirectResponse(url="/cosmetic/cosmetic_received?success=Device+sent+to+Cosmetic+Received", status_code=302)


# ── Revalidate IQC (item 12 — Final QC page redesign) ───────────────────────
# Editable re-entry of the physical inspection checklist, callable from the
# Final QC page's "Revalidate IQC" tab so QC can correct a defect the
# original IQC pass missed, without leaving Final QC. Whitelisted against
# IQCInspection's real columns (minus identity/system fields) so this can't
# be used to set arbitrary attributes.
IQC_REVALIDATE_FIELDS = [
    "power_on", "bios_password", "all_ok", "status",
    "screen_dot", "screen_line", "screen_functional", "screen_discoloration",
    "screen_patch", "screen_broken", "screen_flickering", "screen_scratch",
    "screen_loose", "screen_missing", "screen_hinge_broken", "screen_colour_spread",
    "screen_keyboard_mark", "screen_hard_press",
    "panel_a_scratch", "panel_a_broken", "panel_a_missing", "panel_a_dent", "panel_a_colour_fade",
    "panel_b_scratch", "panel_b_colour_fade", "panel_b_rubber_cut", "panel_b_broken", "panel_b_missing",
    "panel_c_scratch", "panel_c_broken", "panel_c_missing", "panel_c_dent", "panel_c_colour_fade",
    "panel_d_dent", "panel_d_colour_fade", "panel_d_scratch", "panel_d_broken", "panel_d_missing",
    "keyboard_working", "keyboard_colour_fade", "keyboard_key_missing", "keyboard_hard_press",
    "speaker_status",
    "touchpad_working", "touchpad_click_working", "touchpad_scratch", "touchpad_colour_fade", "touchpad_missing",
    "port_hdmi", "port_usb_working", "port_audio_jack", "usb_a_ports", "usb_c_ports", "ethernet_ports",
    "wifi_status", "webcam_status", "hdd_connector", "hdd_casing", "battery_present", "battery_cable",
    "charging_port", "dvd_drive",
    "cover_ram", "cover_dvd", "cover_storage",
    "hinge_condition", "hinge_cover", "touchpad_logicboard",
    "storage_health_pct", "fan_sound_dba", "fan_working",
    "remarks",
]


@router.post("/revalidate-iqc")
async def revalidate_iqc(
    request: Request,
    barcode: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("cosmetic_finalqc", "edit")),
):
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")
    iqc = (await db.execute(
        select(IQCInspection).where(IQCInspection.device_id == device.id)
    )).scalar_one_or_none()
    if not iqc:
        raise HTTPException(404, "No IQC inspection record found for this device")

    form = await request.form()
    changed = {}
    for field in IQC_REVALIDATE_FIELDS:
        if field not in form:
            continue
        new_val = form.get(field).strip() or None
        if getattr(iqc, field) != new_val:
            setattr(iqc, field, new_val)
            changed[field] = new_val
    if changed:
        await audit(db, action="IQC_REVALIDATED", user=current_user,
                    table_name="iqc_inspections", record_id=str(iqc.id),
                    new_value=changed, request=request)
        await db.commit()
    return RedirectResponse(
        url=f"/cosmetic/final_qc?success=IQC+revalidated+for+{barcode}#dev-{device.id}",
        status_code=302)


@router.post("/{barcode}/fail")
async def cosmetic_fail_assign(
    barcode: str,
    engineer_user_id: str = Form(...),
    notes: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """'Fail' on the Cosmetic Received / Cleaning / Water Sanding / Cosmetic
    Completed pages — assign the device to an L1/L2 engineer. Same
    "Fail — Assign to L1/L2 Engineer" modal and the same WorkOrder +
    stage-move mechanism as the Stress Test page's own Fail action
    (routers/stress_api.py stress_fail_assign) — kept as a separate endpoint
    rather than reused directly so the note/notification wording says what
    actually failed (a cosmetic stage, not a stress test) and lands in
    device.repair_notes (the shared Repair Notes field) rather than the
    stress-test-specific device.stress_notes.
    """
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")
    if device.current_stage not in (DeviceStage.cosmetic_received, DeviceStage.cleaning,
                                     DeviceStage.water_sanding, DeviceStage.cosmetic_completed):
        raise HTTPException(400, "This device is not at Cosmetic Received, Cleaning, Water Sanding or Cosmetic Completed")

    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    module_key = PERM_MODULE_BY_STAGE[device.current_stage]
    if not has_perm(role_val, module_key, "edit"):
        raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the {module_key} module.")

    try:
        eng_uuid = uuid_module.UUID(engineer_user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "Invalid engineer selected")

    engineer = (await db.execute(select(User).where(User.id == eng_uuid))).scalar_one_or_none()
    if not engineer or engineer.role not in (UserRole.l1_engineer, UserRole.l2_engineer):
        raise HTTPException(400, "Selected user is not an active L1/L2 engineer")

    from_stage_label = STAGE_LABELS.get(device.current_stage, device.current_stage.value)
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
    # Same fresh-cycle reset as the Stress Test page's Fail action — a device
    # returning from a cosmetic fail starts a new repair cycle.
    device.l1l2_status = "New"
    device.l34_status = None
    device.updated_at = app_now()

    repair_note = f"{from_stage_label} Failed — assigned to {engineer.full_name or engineer.username}"
    if notes:
        repair_note += f". {notes}"
    device.repair_notes = repair_note

    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.l1,
                         moved_by=current_user.username, notes=repair_note))

    work_id = await _gen_work_id(db)
    db.add(WorkOrder(
        work_id=work_id, device_id=device.id, barcode=device.barcode,
        stage="l1", assigned_role=engineer.role.value,
        assigned_user_id=engineer.id, assigned_username=engineer.username,
        assigned_name=engineer.full_name, status="pending",
        created_by=current_user.username,
    ))

    await create_notification(
        db, user_id=engineer.id, title="Device Assigned to You",
        message=(f"{device.barcode} failed {from_stage_label} and has been assigned to you for "
                 f"L1/L2 repair (WorkID: {work_id})."),
        notification_type="warning",
        barcode=device.barcode, brand=device.brand, model=device.model,
        stage=DeviceStage.l1.value,
    )

    await audit(db, user=current_user, action="COSMETIC_FAIL_ASSIGNED",
                table_name="devices", record_id=str(device.id),
                new_value={"from_stage": prev_stage.value, "assigned_to": engineer.username,
                           "work_id": work_id, "notes": notes},
                request=None)

    await db.commit()
    return RedirectResponse(
        url=f"/cosmetic/{prev_stage.value}?success=Device+failed+%26+assigned+to+" +
            (engineer.full_name or engineer.username).replace(" ", "+"),
        status_code=302,
    )


async def _paint_fail_painter(db: AsyncSession, device) -> User | None:
    """Painting user to send a Paint Fail tag back to (2026-10-24):
      1. Cosmetic Flow (All Tags -> Flow Data): the row whose Water Sanding
         user is the one holding this tag now (its pending "water" WorkID,
         else nobody) -> that row's Painting user;
      2. otherwise whoever last had this tag's Painting WorkID.
    Only an active user is returned."""
    water_wo = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device.id,
                                WorkOrder.stage == MOVE_STAGE_CODE[DeviceStage.water_sanding],
                                WorkOrder.assigned_user_id.isnot(None))
        .order_by(WorkOrder.assigned_at.desc())
    )).scalars().first()
    candidates = []
    if water_wo:
        row = (await db.execute(
            select(CosmeticFlowRow).where(CosmeticFlowRow.water_sanding_user_id == water_wo.assigned_user_id,
                                          CosmeticFlowRow.painting_user_id.isnot(None))
            .order_by(CosmeticFlowRow.created_at)
        )).scalars().first()
        if row:
            candidates.append(row.painting_user_id)
    paint_wo = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device.id,
                                WorkOrder.stage == MOVE_STAGE_CODE[DeviceStage.painting],
                                WorkOrder.assigned_user_id.isnot(None))
        .order_by(WorkOrder.assigned_at.desc())
    )).scalars().first()
    if paint_wo:
        candidates.append(paint_wo.assigned_user_id)
    for uid in candidates:
        user = (await db.execute(select(User).where(User.id == uid, User.status == True))).scalar_one_or_none()  # noqa: E712
        if user:
            return user
    return None


@router.post("/{barcode}/paint-fail")
async def cosmetic_paint_fail(
    barcode: str,
    notes: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """'Paint Fail' on Water Sanding (2026-10-24): back to Painting, assigned
    to the painter the Cosmetic Flow gives (see _paint_fail_painter), with a
    new Painting WorkID. The Water Sanding WorkID is closed."""
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")
    if device.current_stage != DeviceStage.water_sanding:
        raise HTTPException(400, "This device is not at Water Sanding.")
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    module_key = PERM_MODULE_BY_STAGE[DeviceStage.water_sanding]
    if not has_perm(role_val, module_key, "edit"):
        raise HTTPException(403, f"Your role ({role_val}) does not have 'edit' permission for the {module_key} module.")

    painter = await _paint_fail_painter(db, device)
    if not painter:
        raise HTTPException(400, "No Painting user found for this tag (no Cosmetic Flow row and no earlier "
                                 "Painting WorkID). Use Move / Assign instead.")

    prev_stage = device.current_stage
    await db.execute(
        update(WorkOrder)
        .where(WorkOrder.device_id == device.id,
               WorkOrder.stage == MOVE_STAGE_CODE[DeviceStage.water_sanding],
               WorkOrder.status == "pending")
        .values(status="completed", completed_at=app_now())
    )
    prev_mv = (await db.execute(
        select(StageMovement).where(StageMovement.device_id == device.id,
                                    StageMovement.to_stage == prev_stage,
                                    StageMovement.exited_at == None)  # noqa: E711
        .order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()

    painter_name = painter.full_name or painter.username
    note = f"Paint Fail — sent back to Painting, assigned to {painter_name}"
    if (notes or "").strip():
        note += f". {notes.strip()}"
    device.current_stage = DeviceStage.painting
    device.updated_at = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.painting,
                         moved_by=current_user.username, notes=note))

    work_id = await _gen_work_id(db)
    painter_role = painter.role.value if hasattr(painter.role, "value") else str(painter.role)
    db.add(WorkOrder(
        work_id=work_id, device_id=device.id, barcode=device.barcode,
        stage=MOVE_STAGE_CODE[DeviceStage.painting], assigned_role=painter_role,
        assigned_user_id=painter.id, assigned_username=painter.username,
        assigned_name=painter.full_name, status="pending", created_by=current_user.username,
    ))
    await create_notification(
        db, user_id=painter.id, title="Paint Fail — Device Back to You",
        message=f"{device.barcode} failed paint at Water Sanding and is back in Painting for you (WorkID: {work_id}).",
        notification_type="warning",
        barcode=device.barcode, brand=device.brand, model=device.model,
        stage=DeviceStage.painting.value,
    )
    await audit(db, user=current_user, action="COSMETIC_PAINT_FAIL",
                table_name="devices", record_id=str(device.id),
                old_value={"stage": prev_stage.value},
                new_value={"stage": DeviceStage.painting.value, "assigned_to": painter.username,
                           "work_id": work_id, "notes": notes},
                request=None)
    await db.commit()
    return JSONResponse({"ok": True, "work_id": work_id, "assigned_to": painter_name})


@router.post("/bulk-assign")
async def bulk_assign(
    barcodes: str = Form(...),
    engineer_user_id: str = Form(...),
    notes: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin / Cosmetic Manager bulk (re)assignment on Cosmetic Received and
    Cleaning/Putty/Dry Sanding/Masking/Painting/Water Sanding: check a batch
    of tags, pick a user, and each selected tag gets a fresh WorkID for its
    CURRENT stage — same MOVE_STAGE_CODE the page's own WorkID column reads,
    so the new assignment shows up immediately without moving the tag
    anywhere. Unlike Move/Fail this never changes device.current_stage or
    writes a StageMovement — it is a pure reassignment.

    Cosmetic Received is the one exception (2026-09-15): its per-row "Move to
    Cleaning" button already combines assign + advance in one action, so
    Assign staying a pure reassignment there just meant a selected batch
    never actually left Cosmetic Received — read as "tags aren't moving to
    Cleaning or getting assigned". A device found at DeviceStage.cosmetic_received
    below also advances to Cleaning, exactly like advance_stage's own
    ASSIGN_ON_MOVE_STAGES branch does for a single tag. Devices at the other
    6 stages are untouched — pure reassignment, as before.
    """
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if role_val not in ("admin", "cosmetic_manager"):
        raise HTTPException(403, "Bulk Assign is Admin / Cosmetic Manager only")

    barcode_list = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not barcode_list:
        raise HTTPException(400, "No tags selected")

    try:
        eng_uuid = uuid_module.UUID(engineer_user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "Invalid user selected")
    engineer = (await db.execute(
        select(User).where(User.id == eng_uuid, User.status == True)
    )).scalar_one_or_none()
    if not engineer:
        raise HTTPException(400, "Selected user is not an active user")
    eng_role_val = engineer.role.value if hasattr(engineer.role, "value") else str(engineer.role)

    devices = (await db.execute(
        select(Device).where(Device.barcode.in_(barcode_list))
    )).scalars().all()
    if not devices:
        raise HTTPException(404, "No matching tags found")

    work_ids = []
    moved_to_cleaning = 0
    for device in devices:
        if device.current_stage not in BULK_ASSIGN_STAGES:
            continue  # a tag that moved on since the checkbox was ticked — skip, don't fail the whole batch

        advance_to_cleaning = device.current_stage == DeviceStage.cosmetic_received
        if advance_to_cleaning:
            db.add(StageMovement(
                device_id=device.id, from_stage=device.current_stage, to_stage=DeviceStage.cleaning,
                moved_by=current_user.username,
                notes=notes or "Advanced from Cosmetic Received to Cleaning (bulk Assign)",
            ))
            device.current_stage = DeviceStage.cleaning
            device.updated_at = app_now()
            moved_to_cleaning += 1

        stage_code = MOVE_STAGE_CODE[device.current_stage]
        work_id = await _gen_work_id(db)
        db.add(WorkOrder(
            work_id=work_id, device_id=device.id, barcode=device.barcode,
            stage=stage_code, assigned_role=eng_role_val,
            assigned_user_id=engineer.id, assigned_username=engineer.username,
            assigned_name=engineer.full_name, status="pending",
            created_by=current_user.username,
        ))
        work_ids.append(work_id)
        message = (f"{device.barcode} moved to {STAGE_LABELS.get(device.current_stage, device.current_stage.value)} "
                   f"and has been assigned to you (WorkID: {work_id})."
                   if advance_to_cleaning else
                   f"{device.barcode} has been assigned to you at "
                   f"{STAGE_LABELS.get(device.current_stage, device.current_stage.value)} "
                   f"(WorkID: {work_id}).")
        await create_notification(
            db, user_id=engineer.id, title="Device Assigned to You",
            message=message,
            notification_type="info",
            barcode=device.barcode, brand=device.brand, model=device.model,
            stage=device.current_stage.value,
        )

    if not work_ids:
        raise HTTPException(400, "None of the selected tags are still on one of these stages")

    # record_id is String(50) — a batch of device UUIDs joined together
    # overflows it past 1-2 tags, so the full id list goes in new_value
    # instead (record_id is genuinely "which one record", not meaningful for
    # a bulk action across many).
    await audit(db, user=current_user, action="COSMETIC_BULK_ASSIGNED",
                table_name="devices", record_id=None,
                new_value={"assigned_to": engineer.username, "work_ids": work_ids,
                           "device_ids": [str(d.id) for d in devices],
                           "count": len(work_ids), "moved_to_cleaning": moved_to_cleaning,
                           "notes": notes},
                request=None)

    await db.commit()
    return JSONResponse({"ok": True, "assigned": len(work_ids), "work_ids": work_ids,
                          "moved_to_cleaning": moved_to_cleaning})
