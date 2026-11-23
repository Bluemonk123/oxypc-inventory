"""Rebuild old IQC history (2026-11-22, Pankaj) — NEW / SOLD IQC copies for
tags and sales from before iqc_snapshots existed (services/iqc_snapshot.py
writes them from go-live on).

Start from the tag's live IQC (TRC IQC) and walk its logged changes back:

  NEW   undo every logged change made after the tag left intake
        (GRN / IQC / Stock In);
  SOLD  for each sale with no copy, undo the changes made after sold_at.

What the logs allow:
  DEVICE_SPECS_QUICK_EDIT   old value kept           -> undone exactly
  DEVICE_SPEC_FROM_PARTS    old RAM / HDD text kept  -> undone (size parsed from text)
  IQC_REVALIDATED           new value only           -> field "not recorded" in the copy
Edits that keep no log (Final QC make / model / CPU / grade, Stress RAM,
admin Device Edit) can't be seen, so those fields keep today's value.

The repair line never writes the IQC checklist (parts changes live in
part_requests), so for most tags the live checklist IS the original IQC.

Rebuilt copies are written with backfilled=True; `context.unknown_fields`
lists {field: "changed <dd-mm-yyyy>"} for the not-recorded ones. Nothing is
overwritten — tags / sales that already have a copy are skipped.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from sqlalchemy import select

from models.device import Device, DeviceStage, StageMovement
from models.engines import AuditLog
from models.iqc_inspection import IQCInspection
from models.iqc_snapshot import IQCSnapshot
from models.sales import Sale
from services.iqc_snapshot import _context, _iqc_payload
from utils.timezone import app_now

_INTAKE = {DeviceStage.grn, DeviceStage.iqc, DeviceStage.stock_in}
_ACTIONS = ("DEVICE_SPECS_QUICK_EDIT", "DEVICE_SPEC_FROM_PARTS", "IQC_REVALIDATED")
_CHUNK = 500


def _gb_total(text):
    """'8GB DDR4, 8GB DDR4' -> 16; '1 TB SSD' -> 1024; '—' / '' -> None."""
    total = 0
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(TB|GB)", text or "", flags=re.I):
        total += float(n) * (1024 if unit.upper() == "TB" else 1)
    return int(total) or None


def _parts_undo(values):
    """DEVICE_SPEC_FROM_PARTS old texts -> exact device field values."""
    out = {}
    for part, text in (values or {}).items():
        text = None if (text or "").strip() in ("", "—", "-") else text.strip()
        gb = _gb_total(text)
        plain = bool(text) and re.fullmatch(r"\d+(\.\d+)?\s*(GB|TB)", text, flags=re.I)
        if part == "RAM":
            out.update({"ram_gb": gb, "ram_summary": None if plain else text,
                        "total_ram_size": f"{gb} GB" if gb else None})
        elif part == "Hard Drive":
            out.update({"storage_gb": gb, "hdd_summary": text,
                        "total_hdd_size": f"{gb} GB" if gb else None})
    return out


def _load(v):
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def _events(rows, insp_dev):
    """{device_id(str): [(ts, exact{field: old}, unknown[fields])]} oldest first."""
    ev = defaultdict(list)
    for action, rid, old, new, ts in rows:
        if action == "IQC_REVALIDATED":
            did = insp_dev.get(rid)
            if did:
                ev[did].append((ts, {}, [k for k in _load(new) if k != "barcode"]))
            continue
        if action == "DEVICE_SPECS_QUICK_EDIT":
            exact = {k: v for k, v in _load(old).items() if k != "barcode"}
        else:
            exact = _parts_undo(_load(old).get("values"))
        if exact:
            ev[rid].append((ts, exact, []))
    for v in ev.values():
        v.sort(key=lambda e: e[0])
    return ev


def _roll_back(payload, events, after):
    """Undo (newest first) every event later than `after`; returns unknown {field: note}."""
    unknown = {}
    for ts, exact, unk in reversed(events):
        if after is not None and ts <= after:
            break
        for k in unk:
            part = "device" if k in payload["device"] else "inspection"
            payload[part][k] = None
            unknown[k] = "changed " + ts.strftime("%d-%m-%Y")
        for k, v in exact.items():
            part = "device" if k in payload["device"] else "inspection"
            payload[part][k] = v
            unknown.pop(k, None)
    return unknown


def _ctx(session, device, unknown, stage):
    ctx = _context(session, device)
    for k in ("location", "location_zone"):     # today's location, not then
        ctx.pop(k, None)
    ctx["stage"] = stage
    if unknown:
        ctx["unknown_fields"] = unknown
    return ctx


def _plan(session):
    """Runs on a sync Session: what is left to rebuild (re-read on every run,
    so a stopped or repeated run carries on where it left off)."""
    from sqlalchemy import func
    have_new = set(session.execute(select(IQCSnapshot.device_id).where(IQCSnapshot.kind == "NEW")).scalars())
    have_sold = set(session.execute(select(IQCSnapshot.sale_id).where(IQCSnapshot.kind == "SOLD")).scalars())
    insp_rows = session.execute(select(IQCInspection.id, IQCInspection.device_id)).all()
    insp_dev = {str(i): str(d) for i, d in insp_rows}
    with_iqc = {d for _i, d in insp_rows}
    sales = defaultdict(list)
    for sid, did, sold_at, sno in session.execute(
            select(Sale.id, Sale.device_id, Sale.sold_at, Sale.sale_number).order_by(Sale.sold_at)).all():
        if sid not in have_sold and did in with_iqc:
            sales[did].append((sid, sold_at, sno))
    todo = sorted({d for d in with_iqc if d not in have_new} | set(sales), key=str)
    ev = _events(session.execute(
        select(AuditLog.action, AuditLog.record_id, AuditLog.old_value, AuditLog.new_value, AuditLog.timestamp)
        .where(AuditLog.action.in_(_ACTIONS))).all(), insp_dev)
    left = dict(session.execute(       # when each tag first left intake
        select(StageMovement.device_id, func.min(StageMovement.moved_at))
        .where(StageMovement.to_stage.notin_(list(_INTAKE)))
        .group_by(StageMovement.device_id)).all())
    return {"todo": todo, "sales": sales, "ev": ev, "left": left,
            "new_left": len(with_iqc - have_new), "sold_left": sum(len(v) for v in sales.values())}


def _chunk(session, ids, plan, apply: bool):
    """Rebuild one batch of tags (sync Session). Re-checks what already
    exists, so nothing is ever written twice."""
    n = {"new": 0, "sold": 0, "not_recorded": 0, "rolled_back": 0}
    have_new = set(session.execute(select(IQCSnapshot.device_id).where(
        IQCSnapshot.kind == "NEW", IQCSnapshot.device_id.in_(ids))).scalars())
    have_sold = set(session.execute(select(IQCSnapshot.sale_id).where(
        IQCSnapshot.kind == "SOLD", IQCSnapshot.device_id.in_(ids))).scalars())
    devs = {d.id: d for d in session.execute(select(Device).where(Device.id.in_(ids))).scalars()}
    insps = {x.device_id: x for x in session.execute(
        select(IQCInspection).where(IQCInspection.device_id.in_(ids))).scalars()}
    for did in ids:
        device, insp = devs.get(did), insps.get(did)
        if device is None or insp is None:
            continue
        events = plan["ev"].get(str(did), [])
        if did not in have_new:
            payload = _iqc_payload(device, insp)
            cut = plan["left"].get(did)      # None = never left intake: live IQC is the NEW one
            unknown = _roll_back(payload, events, cut) if cut else {}
            n["new"] += 1
            n["not_recorded"] += bool(unknown)
            n["rolled_back"] += bool(cut) and any(e[0] > cut for e in events)
            if apply:
                session.add(IQCSnapshot(
                    device_id=did, kind="NEW", taken_at=insp.inspected_at or device.created_at or app_now(),
                    taken_by=None, source="Rebuilt from history", backfilled=True,
                    iqc=payload, context=_ctx(session, device, unknown, "iqc")))
        for sid, sold_at, sno in plan["sales"].get(did, []):
            if sid in have_sold:
                continue
            payload = _iqc_payload(device, insp)
            unknown = _roll_back(payload, events, sold_at) if sold_at else {}
            n["sold"] += 1
            if apply:
                session.add(IQCSnapshot(
                    device_id=did, kind="SOLD", sale_id=sid, taken_at=sold_at or app_now(),
                    taken_by=None, source=("Rebuilt " + (sno or ""))[:40], backfilled=True,
                    iqc=payload, context=_ctx(session, device, unknown, "sold")))
    if apply:
        session.flush()
    return n


# ── Background run (2026-11-23): production has ONE app worker, so a single
# 89k-record pass inside the click blocked every user and was cut off by the
# proxy after 2 minutes (rolled back). Now it runs as a background task in
# batches of 300 tags, each saved on its own and followed by a pause, so the
# app keeps serving and the card's counts drop as it goes. ──
_BATCH = 300
STATE = {"running": False, "done": 0, "total": 0, "new": 0, "sold": 0, "not_recorded": 0,
         "rolled_back": 0, "error": None, "started_at": None, "finished_at": None, "by": None}


async def run_rebuild(by: str = "system", pause: float = 0.2):
    """The whole rebuild, batch by batch (each batch committed). Safe to run
    again: it only adds copies that are still missing."""
    import asyncio
    from database import AsyncSessionLocal
    if STATE["running"]:
        return STATE
    STATE.update(running=True, done=0, total=0, new=0, sold=0, not_recorded=0, rolled_back=0,
                 error=None, started_at=app_now(), finished_at=None, by=by)
    try:
        async with AsyncSessionLocal() as db:
            plan = await db.run_sync(_plan)
        todo = plan["todo"]
        STATE["total"] = len(todo)
        for i in range(0, len(todo), _BATCH):
            ids = todo[i:i + _BATCH]
            async with AsyncSessionLocal() as db:
                n = await db.run_sync(lambda s: _chunk(s, ids, plan, True))
                await db.commit()
            for k in ("new", "sold", "not_recorded", "rolled_back"):
                STATE[k] += n[k]
            STATE["done"] = min(i + _BATCH, len(todo))
            await asyncio.sleep(pause)          # let other requests run
        async with AsyncSessionLocal() as db:
            from services.audit_engine import audit
            await audit(db, action="IQC_HISTORY_REBUILT", table_name="iqc_snapshots",
                        record_id=f"{STATE['new']} NEW / {STATE['sold']} SOLD"[:50],
                        new_value={k: STATE[k] for k in ("new", "sold", "not_recorded", "rolled_back", "total")},
                        notes=f"Old IQC history rebuilt by {by} (background, batches of {_BATCH}). "
                              "Reverse: delete from iqc_snapshots where backfilled = true.")
            await db.commit()
    except Exception as e:                       # keep what was saved; show the error on the card
        STATE["error"] = f"{type(e).__name__}: {e}"[:300]
        print(f"[IQC rebuild] stopped: {STATE['error']}")
    finally:
        STATE["running"] = False
        STATE["finished_at"] = app_now()
    return STATE


async def preview(db):
    plan = await db.run_sync(_plan)
    return {"tags": len(plan["todo"]), "new": plan["new_left"], "sold": plan["sold_left"]}


async def counts(db):
    """Cheap numbers for the Data Fixes card: tags with an IQC but no NEW copy,
    sales of such tags with no SOLD copy."""
    from sqlalchemy import func, exists, and_
    no_new = (await db.execute(select(func.count()).select_from(IQCInspection).where(~exists().where(and_(
        IQCSnapshot.device_id == IQCInspection.device_id, IQCSnapshot.kind == "NEW"))))).scalar() or 0
    no_sold = (await db.execute(select(func.count()).select_from(Sale).where(
        exists().where(IQCInspection.device_id == Sale.device_id),
        ~exists().where(and_(IQCSnapshot.sale_id == Sale.id, IQCSnapshot.kind == "SOLD"))))).scalar() or 0
    return {"new": no_new, "sold": no_sold}
