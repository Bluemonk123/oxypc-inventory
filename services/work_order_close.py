"""Close a tag's open WorkOrders when the tag is handed to someone else.

WorkID Status shows one row per WorkOrder, and a row only gets a Completed Date
once its WorkOrder is closed. Reassigning a tag (Change Engineer, Assign /
Bulk Assign to L1/L2, bucket / Final-QC-fail assignment) creates a NEW WorkOrder
for the new engineer but used to leave the previous engineer's one "pending"
forever, so that engineer's row never got a Completed Date (found 2026-10-07).

Call this just before adding the new WorkOrder: the previous assignee's WorkID
completes at this moment, and the new assignee's WorkID starts at the same
moment, so the hand-over reads as a clean timeline.
"""
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.work_order import WorkOrder
from utils.timezone import app_now

# A WorkOrder is OPEN unless its status is one of these (2026-10-23).
# "reassigned" = the tag was taken off this engineer before they finished it
# (Pick This, Change Engineer, Customise / manual Move to Stage) — closed, but
# NOT the engineer's completed work. completed_at holds the moment it closed
# either way. Every "open WorkOrders" query must use wo_open(), not
# `status != "completed"`, or reassigned rows would reappear in queues.
CLOSED_WO_STATUSES = ("completed", "reassigned")


def wo_open():
    """SQL condition: the WorkOrder is still open."""
    return WorkOrder.status.notin_(CLOSED_WO_STATUSES)


async def close_open_work_orders(db: AsyncSession, device_id: uuid.UUID, at=None,
                                 keep_id: uuid.UUID | None = None) -> int:
    """Mark every open (non-"asgn") WorkOrder of the device completed at `at`
    (default now). Returns how many were closed. `keep_id` leaves that one
    WorkOrder open (the reused STRS- row, see reuse_open_stress_work_order).

    "asgn" rows are /transfers' plain assignment bookkeeping, not pipeline work,
    and are left alone (the WorkID Status page hides them)."""
    return await _close(db, device_id, "completed", at, keep_id)


async def reassign_open_work_orders(db: AsyncSession, device_id: uuid.UUID, at=None,
                                    keep_id: uuid.UUID | None = None) -> int:
    """The tag is being taken off its current engineer(s) before they finished:
    close every open (non-"asgn") WorkOrder as "reassigned" (not completed).
    Used by Pick This (L1/L2, L3/L4), Change Engineer and stage moves from the
    Customise modal / Move Device page."""
    return await _close(db, device_id, "reassigned", at, keep_id)


async def _close(db, device_id, status, at, keep_id) -> int:
    conds = [WorkOrder.device_id == device_id, wo_open(), WorkOrder.stage != "asgn"]
    if keep_id is not None:
        conds.append(WorkOrder.id != keep_id)
    result = await db.execute(
        update(WorkOrder).where(*conds)
        .values(status=status, completed_at=at or app_now())
    )
    return result.rowcount or 0


async def reuse_open_stress_work_order(db: AsyncSession, device_id: uuid.UUID, new_stage,
                                       user, created_by: str | None = None,
                                       source_transfer_id=None):
    """Assignment to a tag at L1/L2, L3/L4, Stress Test or Final QC (bucket
    assign, single / bulk assign, Change Engineer) — one WorkID per visit
    (2026-10-27, was Stress only since 2026-10-17). The stage's open pending
    WorkID is given to `user` (assigned date = now, movement date kept); one
    already held by someone else closes as completed and a new one opens for
    `user` (services/stage_work_orders.open_for_stage). Returns that WorkID,
    so the caller skips creating its own and closes the tag's OTHER open
    WorkIDs with keep_id=it. Any other stage: None, as before."""
    from models.device import Device
    from services.stage_work_orders import open_for_stage, pick_stage_of
    if user is None or pick_stage_of(new_stage) is None:
        return None
    device = await db.get(Device, device_id)
    if device is None:
        return None
    return await open_for_stage(db, device, new_stage, assignee=user, created_by=created_by,
                                source_transfer_id=source_transfer_id)


# ── Stress Test result closes the Stress WorkID (2026-10-27) ────────────────
# A tag's Stress-step WorkIDs: the "STRS-" one L1/L2 Mark Complete raises
# (stage "qc"), a bucket assignment to Stress Test (stage "qc") and Change
# Engineer to Stress Test (stage "qc_ch"). Submitting the Stress result (Pass
# or Fail, routers/stress_api.py) used to add a separate, already-completed
# "strs" WorkID for the submitter and leave these open forever — two Stress
# WorkIDs per visit, one never closed, so the Stress engineer's open count
# kept growing after the tags had moved on.
STRESS_STEP_CODES = ("qc", "qc_ch")


def stress_step_clause():
    """SQL condition: a Stress-step WorkOrder (not the "strs" result record)."""
    from sqlalchemy import or_
    return or_(WorkOrder.work_id.like("STRS-%"), WorkOrder.stage.in_(STRESS_STEP_CODES))


async def complete_open_stress_work_order(db: AsyncSession, device_id: uuid.UUID, user,
                                          at=None) -> WorkOrder | None:
    """The Stress result was submitted by `user`: close the tag's open Stress
    WorkID as completed, credited to `user` (who ran the test), and return
    it — one Stress WorkID per visit. Any older open Stress WorkIDs of the tag
    are closed with it. None when the tag had no open Stress WorkID (the
    caller then records a "strs" result WorkID as before)."""
    at = at or app_now()
    rows = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device_id, wo_open(), stress_step_clause())
        .order_by(WorkOrder.created_at.desc())
    )).scalars().all()
    if not rows:
        return None
    wo = rows[0]
    if wo.assigned_user_id != user.id:
        wo.assigned_user_id = user.id
        wo.assigned_username = user.username
        wo.assigned_name = user.full_name
        wo.assigned_role = getattr(user.role, "value", user.role)
        if wo.assigned_at is None:
            wo.assigned_at = at
    for w in rows:
        w.status = "completed"
        w.completed_at = at
    return wo
