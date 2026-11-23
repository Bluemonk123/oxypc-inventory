"""PNA marks clear themselves once the repair is past (2026-11-22, Pankaj).

A PNA (Part Not Available) mark used to stay until someone unticked it, so a
tag sold and later returned brought its old, already-solved PNA back into
every PNA count (Production Manager tile / summary, WorkID Status, Device
Detail, Part Requests, ...).

Now, when Device.current_stage is set:
  - from L1/L2 to Stress (L1/L2 passed),
  - to Ready for Sale, or
  - to Sold (incl. a customer return repaired straight back to Sold),
every active DevicePNAPart of the tag is cleared — is_active False,
cleared_at now, cleared_by "<user> (auto: <reason>)" — so PNA History keeps
it. L1/L2 -> L3/L4 does NOT clear: the tag is still in repair. Done on the
attribute + flush (like services/repair_location.py), so every code path
that changes a stage is covered.
"""
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from models.device import Device, DeviceStage
from services.repair_location import _norm_stage, current_user_name

_L12 = (DeviceStage.l1, DeviceStage.l2)


def _reason(old, new):
    if new == DeviceStage.sold:
        return "sold"
    if new == DeviceStage.ready_to_sale:
        return "ready for sale"
    if new == DeviceStage.qc_check and old in _L12:
        return "passed L1/L2"
    return None


@event.listens_for(Device.current_stage, "set", active_history=True)
def _mark_pna_clear(target, value, oldvalue, initiator):
    new, old = _norm_stage(value), _norm_stage(oldvalue)
    if new == old:
        return
    why = _reason(old, new)
    if why:
        target.__dict__["_pna_autoclear"] = why


@event.listens_for(Session, "before_flush")
def _clear_pna(session, flush_context, instances):
    todo = [(o, o.__dict__.pop("_pna_autoclear")) for o in list(session.new) + list(session.dirty)
            if isinstance(o, Device) and "_pna_autoclear" in o.__dict__]
    if not todo:
        return
    from models.pna_part import DevicePNAPart
    from utils.timezone import app_now
    now = app_now()
    who = (current_user_name.get() or "system").strip()
    with session.no_autoflush:
        for d, why in todo:
            if d.id is None:
                continue
            session.execute(
                update(DevicePNAPart)
                .where(DevicePNAPart.device_id == d.id, DevicePNAPart.is_active.is_(True))
                .values(is_active=False, cleared_at=now, cleared_by=f"{who} (auto: {why})"[:50])
                .execution_options(synchronize_session=False))
