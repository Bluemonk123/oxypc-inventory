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


# ── Data Fix (2026-10-31): repair-stage tags not on TRC Floor ────────────────
# The hook above only works from now on. Tags already in a repair stage keep
# the location they had (often Showroom). The Data Fixes page lists them and
# moves them to TRC Floor the same way the hook does.

async def trc_floor_plan(db):
    """{"trc": bool, "rows": [{id, tag, stage, location, zone}]} — tags (not
    Trashed) in a repair stage whose current location is not on TRC Floor
    (workshop zone). Current location = latest location log, else
    Device.location_id; no location at all counts as not on TRC Floor."""
    from sqlalchemy import func
    from models.device import STAGE_LABELS
    from models.location import DeviceLocationLog, StorageLocation, ZoneType
    trc_id = (await db.execute(select(StorageLocation.id).where(
        StorageLocation.unit_id == TRC_UNIT_ID))).scalar()
    rn = func.row_number().over(partition_by=DeviceLocationLog.device_id,
                                order_by=DeviceLocationLog.logged_at.desc()).label("rn")
    latest = select(DeviceLocationLog.device_id, DeviceLocationLog.location_id, rn).subquery()
    last = select(latest.c.device_id, latest.c.location_id).where(latest.c.rn == 1).subquery()
    loc_id = func.coalesce(last.c.location_id, Device.location_id)
    q = (select(Device.id, Device.barcode, Device.current_stage, StorageLocation.unit_id, StorageLocation.zone)
         .outerjoin(last, last.c.device_id == Device.id)
         .outerjoin(StorageLocation, StorageLocation.id == loc_id)
         .where(Device.current_stage.in_(list(REPAIR_STAGES)), Device.is_trashed.isnot(True))
         .order_by(Device.current_stage, Device.barcode))
    rows = []
    for did, tag, st, unit, zone in (await db.execute(q)).all():
        if zone == ZoneType.workshop:
            continue
        rows.append({"id": did, "tag": tag, "stage": STAGE_LABELS.get(st, getattr(st, "value", st)),
                     "location": unit or "—",
                     "zone": getattr(zone, "value", zone) or "—"})
    return {"trc": trc_id is not None, "rows": rows}


async def trc_floor_apply(db, user, request=None):
    """Logs each listed tag "moved" to TRC Floor and sets its location_id;
    one audit row with every tag's previous location."""
    from datetime import datetime
    from sqlalchemy import update
    from models.location import DeviceLocationLog, LocationAction, StorageLocation
    from services.audit_engine import audit
    trc_id = (await db.execute(select(StorageLocation.id).where(
        StorageLocation.unit_id == TRC_UNIT_ID))).scalar()
    if trc_id is None:
        return {"ok": False, "detail": "No TRC-FLOOR location exists"}
    plan = await trc_floor_plan(db)
    rows = plan["rows"]
    if not rows:
        return {"ok": True, "moved": 0}
    name = getattr(user, "full_name", None) or getattr(user, "username", None) or "Admin"
    for r in rows:
        db.add(DeviceLocationLog(device_id=r["id"], location_id=trc_id, action=LocationAction.moved,
                                 actor_id=user.id, actor_name=name,
                                 notes=f"Data Fix: in {r['stage']} but at {r['location']} — moved to TRC Floor"))
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 1000):
        await db.execute(update(Device).where(Device.id.in_(ids[i:i + 1000])).values(location_id=trc_id))
    await audit(db, user=user, action="DEVICE_REPAIR_TO_TRC_FLOOR_FIX", table_name="devices",
                record_id=f"{len(rows)} tags",
                new_value={"count": len(rows),
                           "before": {r["tag"]: {"stage": r["stage"], "location": r["location"]} for r in rows}},
                notes="Tags in a repair stage moved to TRC Floor (location log 'moved' + location_id). "
                      "Previous location of each tag is listed in 'before'.",
                request=request)
    await db.commit()
    return {"ok": True, "moved": len(rows)}
