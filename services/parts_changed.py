"""Changed (verified) parts per device — the part-change lifecycle shown on
the Hardware Specification card and as a badge next to the Tag Number on the
pipeline pages.

A part counts as changed once its PartRequest reaches status "received"
(engineer verified it on the Device Profile, or Mark Complete auto-verified
it). Each change then has its own state, worked out from what happened
AFTER it — never from the tag's current stage, so a sold tag that comes back
keeps its history intact:

    pending  verified, no Final QC pass since          -> orange "Changed" badge
    passed   a Final QC pass happened after it          -> value in green
    sold     a sale happened after that pass            -> value in blue

On Final QC pass, pending RAM / Hard Drive changes are written onto the
device's own spec fields (apply_spec_changes_on_pass) so Ready for Sale, the
Model Summary, search and invoices show the real spec. The IQC inspection
record is never modified; the value the device had before is kept in the
DEVICE_SPEC_FROM_PARTS audit entry and shown in the tooltip.

SPEC_ROW maps a PARTS_MATRIX label (services/parts_required.py) to the row
label it belongs to in templates/devices/_iqc_full_report.html. Parts with
no matching row (covers, cables, External Battery, Master Data extras) still
appear in the card's "Updated Parts" list.
"""
from __future__ import annotations

import bisect
import json
import re
from datetime import datetime
from html import escape

from sqlalchemy import select, or_, and_, not_
from sqlalchemy.ext.asyncio import AsyncSession

from models.part_request import PartRequest

SPEC_ROW = {
    # Hardware Specification rows
    "RAM": "RAM",
    "Hard Drive": "Hard Drive",
    "Keyboard": "Keyboard Working?",
    "Internal Battery": "Internal Battery",
    "Battery Cable": "Battery Cable",
    "Camera": "Web Cam",
    "DC Jack": "Charging Port",
    "LAN Port": "Ethernet Ports",
    "USB Port": "USB Ports Working?",
    "HDMI Port": "HDMI Port Working?",
    "Audio Jack": "Audio Jack Working?",
    "Speaker": "Speaker Status",
    "Fan Working": "Fan Working",
    "Motherboard": "Power Status",
    "Touchpad": "Touchpad Working?",
    "Click Button": "Click Function Working?",
    "Wi-Fi Card": "Wi-Fi",
    "DVD Drive": "DVD Drive",
    "HDD Connector": "HDD Connector",
    # Cosmetics section headers
    "Panel": "Display Panel",
    "Bezel": "Bezel Frame",
    "Screen": "Screen/Display",
    "Hinge": "Hinge",
    "Hinge Cover": "Hinge",
    "Bottom Base": "Bottom Base",
    "Touchpad Cover": "Palmrest",
}

# Rows whose new value is written onto the device at Final QC pass.
CAPACITY_PARTS = {"RAM", "Hard Drive"}

_STAGE_LABEL = {"l1": "L1/L2", "l2": "L1/L2", "l3": "L3/L4", "l4": "L3/L4"}
# Orange = changed, waiting for Final QC; green = QC passed; blue = sold.
# "orange" is not a Bootstrap 5 colour — ORANGE below is used inline for it.
STATE_COLOR = {"pending": "orange", "passed": "success", "sold": "primary"}
ORANGE = "#fd7e14"
AUDIT_ACTION = "DEVICE_SPEC_FROM_PARTS"


def _details(r: PartRequest) -> str:
    bits = [r.part_capacity, r.part_type, r.part_make, r.part_model]
    return " ".join(b.strip() for b in bits if b and b.strip())


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%d %b %Y") if dt else "—"


def parse_capacity_gb(text: str | None) -> int | None:
    """'8GB' / '8 GB' / '1TB' / '512' -> GB as int; None when unreadable."""
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb)?", str(text).lower())
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2) == "tb":
        n *= 1024
    return int(round(n)) if n > 0 else None


def _first_after(times: list[datetime], t: datetime) -> datetime | None:
    i = bisect.bisect_right(times, t)
    return times[i] if i < len(times) else None


def classify(t: datetime | None, passes: list[datetime], sales: list[datetime]):
    """(state, pass_at, sold_at) for a change made at time t."""
    if t is None:
        return "pending", None, None
    p = _first_after(passes, t)
    if p is None:
        return "pending", None, None
    s = _first_after(sales, p)
    return ("sold" if s else "passed"), p, s


async def _events(db: AsyncSession, ids):
    """Per device: sorted Final QC pass times and sale times."""
    from models.device import StageMovement, DeviceStage
    from models.sales import Sale

    passes: dict[str, list] = {}
    sales: dict[str, list] = {}
    for did, to_stage, at in (await db.execute(
        select(StageMovement.device_id, StageMovement.to_stage, StageMovement.moved_at)
        .where(StageMovement.device_id.in_(ids),
               StageMovement.to_stage.in_([DeviceStage.final_qc_pass_hold, DeviceStage.sold]))
    )).all():
        if at is None:
            continue
        bucket = passes if to_stage == DeviceStage.final_qc_pass_hold else sales
        bucket.setdefault(str(did), []).append(at)
    # Real sales only — External Tag / Credit Note returns create a ₹0
    # placeholder Sale that never actually sold the device.
    for did, at in (await db.execute(
        select(Sale.device_id, Sale.sold_at)
        .where(Sale.device_id.in_(ids), Sale.sold_at.isnot(None),
               or_(Sale.notes.is_(None),
                   and_(not_(Sale.notes.ilike("Placeholder sale — External%")),
                        not_(Sale.notes.ilike("%Credit Note%")))))
    )).all():
        sales.setdefault(str(did), []).append(at)
    for d in (passes, sales):
        for k in d:
            d[k].sort()
    return passes, sales


async def _applied(db: AsyncSession, ids) -> dict[str, dict]:
    """{part_request_id: {"old": text}} from DEVICE_SPEC_FROM_PARTS audits."""
    from models.engines import AuditLog
    out: dict[str, dict] = {}
    for (nv,) in (await db.execute(
        select(AuditLog.new_value)
        .where(AuditLog.action == AUDIT_ACTION, AuditLog.record_id.in_([str(i) for i in ids]))
    )).all():
        try:
            data = json.loads(nv or "{}")
        except ValueError:
            continue
        for a in data.get("applied", []):
            out[a.get("part_request_id")] = {"old": a.get("old") or "—"}
    return out


async def _received(db: AsyncSession, ids):
    return (await db.execute(
        select(PartRequest)
        .where(PartRequest.device_id.in_(ids), PartRequest.status == "received")
        .order_by(PartRequest.actioned_at.asc().nulls_last(), PartRequest.created_at.asc())
    )).scalars().all()


async def changed_parts_by_device(db: AsyncSession, device_ids) -> dict[str, dict]:
    """{device_id: {"items": [...], "by_key": {spec_row: {...}}}} — only devices
    with at least one changed part get an entry.

    by_key[row] = {"items": [...], "pending": [...], "done": latest non-pending
    item or None} — the card colours the value from "done" and adds the
    Changed badge when anything is still pending."""
    out: dict[str, dict] = {}
    ids = [i for i in device_ids if i]
    if not ids:
        return out
    rows = await _received(db, ids)
    if not rows:
        return out
    passes, sales = await _events(db, ids)
    applied = await _applied(db, ids)
    for r in rows:
        did = str(r.device_id)
        when = r.actioned_at or r.created_at
        state, pass_at, sold_at = classify(when, passes.get(did, []), sales.get(did, []))
        rtype = (r.request_type or "new").lower()
        rtype_label = (f"Downgrade ({r.downgrade_type.title()})"
                       if rtype == "downgrade" and r.downgrade_type else rtype.title())
        ap = applied.get(str(r.id))
        item = {
            "id": str(r.id),
            "part_name": r.part_name,
            "details": _details(r),
            "request_type": rtype_label,
            "stage": _STAGE_LABEL.get((r.stage or "").lower(), (r.stage or "").upper() or "—"),
            "qty": r.qty_handed_over or r.qty_requested or 1,
            "date": _fmt(when),
            "by": r.actioned_by or r.engineer_name or r.requested_by or "—",
            "spec_key": SPEC_ROW.get(r.part_name),
            "state": state,
            "color": STATE_COLOR[state],
            "pass_date": _fmt(pass_at),
            "sold_date": _fmt(sold_at),
            # Written onto the device's own RAM / Storage at Final QC pass
            "applied": bool(ap),
            "old_value": ap["old"] if ap else None,
        }
        status = {"pending": "Pending Final QC",
                  "passed": f"Final QC passed {item['pass_date']}",
                  "sold": f"Sold {item['sold_date']}"}[state]
        item["status_label"] = status
        item["tooltip"] = (
            f"{item['part_name']} changed"
            + (f": {item['details']}" if item["details"] else "")
            + f" — {item['request_type']}, {item['stage']}, {item['date']}, by {item['by']}"
            + f" · {status}"
            + (f" · was {item['old_value']}" if item["old_value"] else "")
        )
        entry = out.setdefault(did, {"items": [], "by_key": {}})
        entry["items"].append(item)
        key = item["spec_key"]
        if key:
            slot = entry["by_key"].setdefault(key, {"items": [], "pending": [], "done": None})
            slot["items"].append(item)
            if state == "pending":
                slot["pending"].append(item)
            else:
                slot["done"] = item          # rows are in time order -> latest wins
    return out


def _badge(icon: str, color: str, title: str) -> str:
    # White disc behind the icon so it stays readable on a highlighted row
    # (e.g. the selected card on Final QC's blue list).
    cls, style = (("", f"color:{ORANGE};") if color == "orange" else (f" text-{color}", ""))
    return (f'<i class="bi {icon}{cls} ms-1 bg-white rounded-circle" '
            f'style="{style}cursor:help;padding:0 1px" '
            f'title="{escape(title)}" aria-label="Parts changed"></i>')


async def parts_badge_map(db: AsyncSession, device_ids) -> dict[str, str]:
    """{device_id: badge HTML} for the Tag Number cell on pipeline pages.

    Pending change -> green arrows; all QC-passed -> green check; the latest
    passed change already sold -> blue check. Hover lists every change."""
    data = await changed_parts_by_device(db, device_ids)
    out = {}
    for did, entry in data.items():
        items = entry["items"]
        lines = []
        for it in items:
            lines.append(f"{it['part_name']}" + (f" → {it['details']}" if it["details"] else "")
                         + f" ({it['stage']}, {it['date']}) — {it['status_label']}")
        title = "Parts changed:\n" + "\n".join(lines)
        if any(it["state"] == "pending" for it in items):
            out[did] = _badge("bi-arrow-repeat", "orange", title)
        elif items[-1]["state"] == "sold":
            out[did] = _badge("bi-check-circle-fill", "primary", title)
        else:
            out[did] = _badge("bi-check-circle-fill", "success", title)
    return out


def _gb_text(gb: int | None) -> str:
    if not gb:
        return ""
    return f"{gb // 1024}TB" if gb >= 1024 and gb % 1024 == 0 else f"{gb}GB"


def _ram_text(device) -> str:
    return device.ram_summary or (f"{device.ram_gb} GB" if device.ram_gb else "—")


def _hdd_text(device) -> str:
    if device.hdd_summary:
        return device.hdd_summary
    bits = [_gb_text(device.storage_gb), device.storage_type or ""]
    return " ".join(b for b in bits if b) or "—"


async def apply_spec_changes_on_pass(db: AsyncSession, device, user=None, request=None) -> list[dict]:
    """Write pending RAM / Hard Drive changes onto the device at Final QC pass.

    "Pending" = verified after the device's previous Final QC pass (or ever,
    if it never passed), so a rework loop never applies the same change
    twice. Replace / Downgrade set the value; New adds to what is there
    (a second stick or drive), or sets it when the device had none.
    Must run BEFORE the pass StageMovement is added. Returns what changed."""
    from models.device import StageMovement, DeviceStage
    from services.audit_engine import audit

    last_pass = (await db.execute(
        select(StageMovement.moved_at)
        .where(StageMovement.device_id == device.id,
               StageMovement.to_stage == DeviceStage.final_qc_pass_hold)
        .order_by(StageMovement.moved_at.desc()).limit(1)
    )).scalar_one_or_none()
    q = select(PartRequest).where(
        PartRequest.device_id == device.id, PartRequest.status == "received",
        PartRequest.part_name.in_(CAPACITY_PARTS))
    rows = (await db.execute(
        q.order_by(PartRequest.actioned_at.asc().nulls_last(), PartRequest.created_at.asc())
    )).scalars().all()
    applied = []
    for r in rows:
        when = r.actioned_at or r.created_at
        if last_pass is not None and when is not None and when <= last_pass:
            continue
        gb = parse_capacity_gb(r.part_capacity)
        if not gb:
            continue
        rtype = (r.request_type or "new").lower()
        ptype = (r.part_type or "").strip() or None
        piece = " ".join(b for b in [_gb_text(gb), ptype, (r.part_make or "").strip() or None] if b)
        if r.part_name == "RAM":
            old = _ram_text(device)
            if rtype == "new" and device.ram_gb:
                device.ram_gb = device.ram_gb + gb
                device.ram_summary = f"{device.ram_summary}, {piece}" if device.ram_summary else None
                try:
                    device.total_ram_count = str(int(device.total_ram_count or 1) + 1)
                except ValueError:
                    pass
            else:
                device.ram_gb = gb
                device.ram_summary = piece
                device.total_ram_count = "1"
            if ptype:
                device.ram_type = ptype
            device.total_ram_size = _gb_text(device.ram_gb)
            new = _ram_text(device)
        else:  # Hard Drive
            old = _hdd_text(device)
            if rtype == "new" and device.storage_gb:
                device.hdd_capacity_gb = gb
                device.hdd_type = ptype
                device.hdd_summary = f"{device.hdd_summary}, {piece}" if device.hdd_summary else None
                try:
                    device.total_hdd_count = str(int(device.total_hdd_count or 1) + 1)
                except ValueError:
                    pass
                device.total_hdd_size = _gb_text((device.storage_gb or 0) + gb)
            else:
                device.storage_gb = gb
                if ptype:
                    device.storage_type = ptype
                device.hdd_summary = piece
                device.total_hdd_count = "1"
                device.total_hdd_size = _gb_text(gb)
            new = _hdd_text(device)
        applied.append({"part_request_id": str(r.id), "part": r.part_name,
                        "request_type": rtype, "old": old, "new": new})
    if applied:
        await audit(db, user=user, action=AUDIT_ACTION, table_name="devices",
                    record_id=str(device.id),
                    old_value={"barcode": device.barcode,
                               "values": {a["part"]: a["old"] for a in applied}},
                    new_value={"barcode": device.barcode, "applied": applied},
                    notes="Final QC pass — RAM/Hard Drive updated from verified part changes",
                    request=request)
    return applied
