"""One WorkID per stage visit — L1/L2, L3/L4, Stress Test, Final QC (2026-10-27).

The life of a WorkID at these four stages, the same everywhere:

  arrive   Any move into the stage (Mark Complete, Request L3/L4, a Fail
           reroute, Customise, Move Device, ...) opens ONE pending WorkID.
           Its created_at is the Movement Date; assigned_at starts equal to it.
           It may arrive unassigned, or already assigned when the hand-off
           named someone (Mark Complete's Stress engineer, Request L3/L4's
           engineer, a bucket / Change Engineer assignment).
  pick     Pick This claims that same pending WorkID: the picker's name goes
           on it and assigned_at becomes the pick time (Movement Date stays).
           Anyone with the stage permission can pick any tag; if someone else
           already holds it, their WorkID closes as COMPLETED and a new one
           opens for the picker (2026-10-27, Pankaj: reassignment completes).
  finish   Complete (Pass / Fail on Stress and Final QC) closes the same
           WorkID. Final QC Pass opens nothing after it.

The Cosmetic stages keep their own flow (assignment on Move / bulk Assign,
routers/cosmetic.py) and are not handled here.
"""
from sqlalchemy import and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.device import DeviceStage
from models.work_order import WorkOrder
from services.work_order_close import wo_open
from utils.timezone import app_now

# stage -> (code a new WorkID gets, WorkID prefix or None, codes / prefixes an
# existing WorkID of this stage may carry, default role)
_SPEC = {
    DeviceStage.l1: ("l1", None, ("l1", "l2"), ("L1L2-",), "l1_engineer"),
    DeviceStage.l3: ("l3", "L3L4-", ("l3",), ("L3L4-",), "l3_engineer"),
    DeviceStage.qc_check: ("qc", "STRS-", ("qc", "qc_ch"), ("STRS-",), "qc_inspector"),
    DeviceStage.final_qc: ("fqc", None, ("fqc", "final"), (), "qc_inspector"),
}
PICK_STAGES = tuple(_SPEC)
_OTHER_PREFIXES = ("L3L4-", "STRS-")


def pick_stage_of(stage):
    """The pick stage a device stage belongs to (L2 counts as L1/L2), else None."""
    if stage == DeviceStage.l2:
        return DeviceStage.l1
    return stage if stage in _SPEC else None


def stage_clause(stage):
    """SQL: a WorkOrder belonging to this pick stage."""
    code, _p, codes, prefixes, _r = _SPEC[stage]
    by_code = WorkOrder.stage.in_(codes)
    own_prefixes = set(prefixes)
    others = [p for p in _OTHER_PREFIXES if p not in own_prefixes]
    if others:
        by_code = and_(by_code, *[not_(WorkOrder.work_id.like(p + "%")) for p in others])
    return or_(by_code, *[WorkOrder.work_id.like(p + "%") for p in prefixes])


async def open_work_orders(db: AsyncSession, device_id, stage):
    """Open WorkOrders of this pick stage, newest first."""
    return (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device_id, wo_open(), stage_clause(stage))
        .order_by(WorkOrder.created_at.desc())
    )).scalars().all()


async def _new_work_id(db: AsyncSession, prefix):
    from routers.repair import _gen_prefixed_work_id, _gen_work_id
    return await (_gen_prefixed_work_id(db, prefix) if prefix else _gen_work_id(db))


def _assign(wo, user, at):
    wo.assigned_user_id = user.id
    wo.assigned_username = user.username
    wo.assigned_name = user.full_name
    wo.assigned_role = getattr(user.role, "value", user.role) or wo.assigned_role
    wo.assigned_at = at


def _unpicked(wo):
    """Nobody holds it (an old WorkID may carry only a username)."""
    return wo.assigned_user_id is None and not (wo.assigned_username or "").strip()


def _complete(wo, at):
    wo.status = "completed"
    wo.completed_at = at


async def open_for_stage(db: AsyncSession, device, stage, *, assignee=None, created_by=None,
                         at=None, requested_by_name=None, source_transfer_id=None):
    """The tag has arrived at `stage` (or is being handed to `assignee` there):
    make sure exactly one open WorkID exists for it and return it. Not a pick
    stage -> None (nothing done).

      - an open unassigned WorkID exists: reuse it (assigning `assignee`, with
        assigned_at = now, when one is given);
      - an open WorkID held by `assignee` already: reuse it unchanged;
      - an open WorkID held by someone else and a new `assignee`: that one
        closes as completed and a new WorkID opens for `assignee`;
      - none open: a new pending WorkID (Movement Date = now)."""
    stage = pick_stage_of(stage)
    if stage is None:
        return None
    at = at or app_now()
    rows = await open_work_orders(db, device.id, stage)
    if rows:
        cur = rows[0]
        for extra in rows[1:]:
            _complete(extra, at)
        if assignee is None:
            return cur
        if _unpicked(cur):
            _assign(cur, assignee, at)
            if source_transfer_id is not None:
                cur.source_transfer_id = source_transfer_id
            return cur
        if cur.assigned_user_id == assignee.id or (
                cur.assigned_user_id is None and cur.assigned_username == assignee.username):
            return cur
        _complete(cur, at)
    code, prefix, _c, _p, role = _SPEC[stage]
    wo = WorkOrder(
        work_id=await _new_work_id(db, prefix), device_id=device.id, barcode=device.barcode,
        stage=code, status="pending", created_at=at, assigned_at=at,
        assigned_role=(getattr(assignee.role, "value", assignee.role) if assignee else role),
        assigned_user_id=assignee.id if assignee else None,
        assigned_username=assignee.username if assignee else None,
        assigned_name=assignee.full_name if assignee else None,
        requested_by_name=requested_by_name, source_transfer_id=source_transfer_id,
        created_by=created_by,
    )
    db.add(wo)
    await db.flush()
    return wo


async def pick(db: AsyncSession, device, stage, user, at=None, requested_by_name=None):
    """Pick This: `user` takes the tag at `stage`. Returns (work_order,
    previous) — previous = [(work_id, user_id, name)] of WorkIDs taken from
    someone else (closed as completed), empty when the pending WorkID was
    simply claimed. (None, []) when the tag is already this user's."""
    stage = pick_stage_of(stage)
    at = at or app_now()
    rows = await open_work_orders(db, device.id, stage)
    if rows and (rows[0].assigned_user_id == user.id or (
            rows[0].assigned_user_id is None and rows[0].assigned_username == user.username)):
        return None, []
    previous = [(w.work_id, w.assigned_user_id, w.assigned_name or w.assigned_username)
                for w in rows if not _unpicked(w)]
    if requested_by_name is None and rows:
        requested_by_name = rows[0].requested_by_name
    wo = await open_for_stage(db, device, stage, assignee=user, created_by=user.username, at=at,
                              requested_by_name=requested_by_name)
    return wo, previous


async def finish(db: AsyncSession, device, stage, user, at=None, *, record_if_none=True):
    """Complete / Pass / Fail at `stage`: close the tag's open WorkID(s) for it
    as completed and return the one that counts. An unpicked WorkID is
    credited to `user` (who did the work); one already held by an engineer
    stays theirs (e.g. a manager submitting on their behalf). With none open,
    a completed WorkID is recorded for `user` (record_if_none)."""
    stage = pick_stage_of(stage)
    at = at or app_now()
    rows = await open_work_orders(db, device.id, stage)
    if rows:
        cur = rows[0]
        if _unpicked(cur):
            _assign(cur, user, at)
        for w in rows:
            _complete(w, at)
        return cur
    if not record_if_none:
        return None
    code, prefix, _c, _p, _r = _SPEC[stage]
    wo = WorkOrder(
        work_id=await _new_work_id(db, prefix), device_id=device.id, barcode=device.barcode,
        stage=code, status="completed", created_at=at, assigned_at=at, completed_at=at,
        assigned_role=getattr(user.role, "value", user.role),
        assigned_user_id=user.id, assigned_username=user.username, assigned_name=user.full_name,
        created_by=user.username,
    )
    db.add(wo)
    await db.flush()
    return wo

