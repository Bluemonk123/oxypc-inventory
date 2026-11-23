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


def _work(session, apply: bool):
    """Runs on a sync Session (AsyncSession.run_sync). Returns the counts."""
    have_new = set(session.execute(select(IQCSnapshot.device_id).where(IQCSnapshot.kind == "NEW")).scalars())
    have_sold = set(session.execute(select(IQCSnapshot.sale_id).where(IQCSnapshot.kind == "SOLD")).scalars())
    insp_rows = session.execute(select(IQCInspection.id, IQCInspection.device_id)).all()
    insp_dev = {str(i): str(d) for i, d in insp_rows}
    with_iqc = {d for _i, d in insp_rows}
    sales = defaultdict(list)
    for s in session.execute(select(Sale).order_by(Sale.sold_at)).scalars():
        if s.id not in have_sold and s.device_id in with_iqc:
            sales[s.device_id].append(s)
    todo = sorted({d for d in with_iqc if d not in have_new} | set(sales), key=str)
    ev = _events(session.execute(
        select(AuditLog.action, AuditLog.record_id, AuditLog.old_value, AuditLog.new_value, AuditLog.timestamp)
        .where(AuditLog.action.in_(_ACTIONS))).all(), insp_dev)
    from sqlalchemy import func
    left = dict(session.execute(       # when each tag first left intake
        select(StageMovement.device_id, func.min(StageMovement.moved_at))
        .where(StageMovement.to_stage.notin_(list(_INTAKE)))
        .group_by(StageMovement.device_id)).all())
    n = {"tags": len(todo), "new": 0, "sold": 0, "not_recorded": 0, "rolled_back": 0}
    for i in range(0, len(todo), _CHUNK):
        ids = todo[i:i + _CHUNK]
        devs = {d.id: d for d in session.execute(select(Device).where(Device.id.in_(ids))).scalars()}
        insps = {x.device_id: x for x in session.execute(
            select(IQCInspection).where(IQCInspection.device_id.in_(ids))).scalars()}
        for did in ids:
            device, insp = devs.get(did), insps.get(did)
            if device is None or insp is None:
                continue
            events = ev.get(str(did), [])
            if did not in have_new:
                payload = _iqc_payload(device, insp)
                cut = left.get(did)          # None = never left intake: live IQC is the NEW one
                unknown = _roll_back(payload, events, cut) if cut else {}
                n["new"] += 1
                n["not_recorded"] += bool(unknown)
                n["rolled_back"] += bool(cut) and any(e[0] > cut for e in events)
                if apply:
                    session.add(IQCSnapshot(
                        device_id=did, kind="NEW", taken_at=insp.inspected_at or device.created_at or app_now(),
                        taken_by=None, source="Rebuilt from history", backfilled=True,
                        iqc=payload, context=_ctx(session, device, unknown, "iqc")))
            for s in sales.get(did, []):
                payload = _iqc_payload(device, insp)
                unknown = _roll_back(payload, events, s.sold_at) if s.sold_at else {}
                n["sold"] += 1
                if apply:
                    session.add(IQCSnapshot(
                        device_id=did, kind="SOLD", sale_id=s.id, taken_at=s.sold_at or app_now(),
                        taken_by=None, source=("Rebuilt " + (s.sale_number or ""))[:40], backfilled=True,
                        iqc=payload, context=_ctx(session, device, unknown, "sold")))
        if apply:
            session.flush()
        session.expunge_all()     # keep memory flat on a big run
    return n


async def preview(db):
    return await db.run_sync(lambda s: _work(s, False))


async def rebuild(db):
    """Writes the copies (caller commits)."""
    return await db.run_sync(lambda s: _work(s, True))


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
