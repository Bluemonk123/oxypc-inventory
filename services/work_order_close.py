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
    """One Stress Test step = one WorkID (2026-10-17).

    L1/L2 Mark Complete already raises an "STRS-" WorkOrder for the Stress
    Test hand-off, usually with no engineer yet. Assigning a Stress Test
    engineer afterwards (bucket assign, single/bulk assign, Change Engineer)
    used to close that and raise a second, numeric WorkID for the same step —
    two Stress IDs on one tag. When the tag is going to Stress Test and such
    an open, still-UNASSIGNED STRS- row exists, this assigns it to `user` and
    returns it so the caller skips creating a new one. An STRS- row already
    held by an engineer is a real hand-over between two people, so it is not
    reused (returns None and the caller proceeds as before)."""
    from models.device import DeviceStage
    if new_stage != DeviceStage.qc_check or user is None:
        return None
    wo = (await db.execute(
        select(WorkOrder).where(
            WorkOrder.device_id == device_id,
            WorkOrder.work_id.like("STRS-%"),
            wo_open(),
            WorkOrder.assigned_user_id.is_(None),
        ).order_by(WorkOrder.created_at.desc())
    )).scalars().first()
    if wo is None:
        return None
    # assigned_role stays as raised by Mark Complete ("qc_inspector").
    wo.assigned_user_id = user.id
    wo.assigned_username = user.username
    wo.assigned_name = user.full_name
    wo.assigned_at = app_now()
    if source_transfer_id is not None:
        wo.source_transfer_id = source_transfer_id
    return wo
