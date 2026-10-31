"""Repair stages put the tag on TRC Floor (2026-10-31).

A tag's location is its latest DeviceLocationLog (else Device.location_id).
Moving a tag into repair never touched it, so a tag assigned out of the
Showroom kept showing "Showroom" on the Dashboard / All Inventory all through
L1/L2, L3/L4, Stress and Final QC.

Now, whenever Device.current_stage is set INTO a repair stage — TRC
Production, L1/L2, L3/L4, Stress (QC Check), any Cosmetic stage, Final QC
(incl. Pass / Fail Hold) — from a stage outside that set, the tag is logged
"moved" to the TRC Floor location (unit TRC-FLOOR) and Device.location_id is
set to it. Moves inside the repair line don't log again, and a tag whose
current location is already on TRC Floor is left alone. Done on the attribute
+ flush, so every one of the code paths that change a stage is covered.

The actor is the signed-in user (auth.dependencies sets current_user_id);
background jobs fall back to the first active admin.
"""
import contextvars

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from models.device import Device, DeviceStage, COSMETIC_STAGES

current_user_id: contextvars.ContextVar = contextvars.ContextVar("current_user_id", default=None)
current_user_name: contextvars.ContextVar = contextvars.ContextVar("current_user_name", default=None)

REPAIR_STAGES = frozenset(
    [DeviceStage.trc_production, DeviceStage.l1, DeviceStage.l2, DeviceStage.l3, DeviceStage.qc_check,
     DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold, *COSMETIC_STAGES])

TRC_UNIT_ID = "TRC-FLOOR"
_cache: dict = {}


def _norm_stage(v):
    if v is None:
        return None
    try:
        return DeviceStage(v.value if hasattr(v, "value") else v)
    except ValueError:
        return None


@event.listens_for(Device.current_stage, "set", active_history=True)
def _mark_repair_entry(target, value, oldvalue, initiator):
    new, old = _norm_stage(value), _norm_stage(oldvalue)
    if new in REPAIR_STAGES and old not in REPAIR_STAGES:
        target.__dict__["_to_trc_floor"] = True


@event.listens_for(Session, "before_flush")
def _log_trc_floor(session, flush_context, instances):
    todo = [o for o in list(session.new) + list(session.dirty)
            if isinstance(o, Device) and o.__dict__.pop("_to_trc_floor", False)]
    if not todo:
        return
    from models.location import DeviceLocationLog, LocationAction, StorageLocation, ZoneType
    from models.user import User
    if "trc" not in _cache:
        _cache["trc"] = session.execute(
            select(StorageLocation.id).where(StorageLocation.unit_id == TRC_UNIT_ID)).scalar()
    trc_id = _cache["trc"]
    if trc_id is None:
        return
    actor = current_user_id.get()
    if actor is None:
        if "admin" not in _cache:
            _cache["admin"] = session.execute(
                select(User.id).where(User.status == True).order_by(User.created_at)  # noqa: E712
            ).scalars().first()
        actor = _cache["admin"]
    if actor is None:
        return
    with session.no_autoflush:
        for d in todo:
            if d.id is None:
                continue
            latest = session.execute(
                select(StorageLocation.zone)
                .select_from(DeviceLocationLog)
                .join(StorageLocation, StorageLocation.id == DeviceLocationLog.location_id)
                .where(DeviceLocationLog.device_id == d.id)
                .order_by(DeviceLocationLog.logged_at.desc()).limit(1)).scalar()
            if latest is None and d.location_id is not None:
                latest = session.execute(select(StorageLocation.zone).where(
                    StorageLocation.id == d.location_id)).scalar()
            if latest == ZoneType.workshop:
                continue
            session.add(DeviceLocationLog(
                device_id=d.id, location_id=trc_id, action=LocationAction.moved, actor_id=actor,
                actor_name=current_user_name.get() or "System",
                notes=f"Auto: entered {(_norm_stage(d.current_stage) or d.current_stage).value} — moved to TRC Floor"))
            d.location_id = trc_id
