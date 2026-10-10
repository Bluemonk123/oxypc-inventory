"""One-time backfill for the changed-parts lifecycle (services/parts_changed.py).

Two steps, run together in ONE transaction:

  1. Close open verify requests — PartRequests at status "handed_over" (part
     handed to the engineer, never verified) are marked "received", EXCEPT
     for tags currently in L1/L2 or L3/L4 (stages l1 / l2 / l3), where the
     engineer can still verify them normally. actioned_at is kept as the
     handover time so the colour lifecycle places them correctly.

  2. Backfill RAM / Hard Drive specs — every verified RAM / Hard Drive change
     that already went through a Final QC pass, but was never written onto
     the device (it happened before the feature shipped), is applied now,
     exactly as a Final QC pass would have done:
        Replace / Downgrade        -> sets the value
        New, device had none       -> sets the value
        New, device already had one-> NOT applied, listed as needs_review
                                      (it may already have been added by hand)
        Device already shows it    -> recorded as applied, value untouched
     Each device gets a DEVICE_SPEC_FROM_PARTS audit entry, so its old value
     shows in the spec-card tooltip ("was 4 GB").

Default is a DRY RUN: it does all the work, prints the summary, writes a CSV
of every row it would touch, then ROLLS BACK. Nothing changes until --apply.

Production (PuTTY):
    cd /opt/oxypc
    sudo docker compose exec oxypc-app python scripts/backfill_changed_parts.py
    sudo docker compose exec oxypc-app python scripts/backfill_changed_parts.py --apply

Re-running is safe: closed requests are no longer "handed_over", and applied
changes are skipped through their audit entries.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from database import AsyncSessionLocal  # noqa: E402
from models.device import Device, DeviceStage  # noqa: E402
from models.part_request import PartRequest  # noqa: E402
from services.audit_engine import audit  # noqa: E402
from services.parts_changed import (  # noqa: E402
    AUDIT_ACTION, CAPACITY_PARTS, _applied, _details as live_details, _events, _gb_text,
    _hdd_text, _ram_text, apply_part_to_device, capacity_and_type, classify,
    effective_label, spare_parts_for,
)


def _gb_label(gb) -> str:
    return _gb_text(gb) or "?"

REPAIR_STAGES = {DeviceStage.l1, DeviceStage.l2, DeviceStage.l3}
STAGE_TEXT = {DeviceStage.l1: "L1/L2", DeviceStage.l2: "L1/L2", DeviceStage.l3: "L3/L4"}


def _stage(d) -> str:
    return getattr(d.current_stage, "value", str(d.current_stage))


def _details(r) -> str:
    return " ".join(b for b in [r.part_capacity, r.part_type, r.part_make, r.part_model] if b)


async def run(apply: bool, out_dir: str) -> int:
    report: list[dict] = []
    async with AsyncSessionLocal() as db:
        # ── Step 1: close open verify requests ───────────────────────────────
        open_rows = (await db.execute(
            select(PartRequest, Device)
            .join(Device, PartRequest.device_id == Device.id)
            .where(PartRequest.status == "handed_over")
        )).all()
        closed, kept = [], 0
        for pr, d in open_rows:
            if d.current_stage in REPAIR_STAGES:
                kept += 1
                report.append({"action": "kept_open_in_repair", "barcode": d.barcode,
                               "stage": STAGE_TEXT.get(d.current_stage, _stage(d)),
                               "part": pr.part_name, "request_type": pr.request_type,
                               "details": _details(pr), "old": "", "new": ""})
                continue
            pr.status = "received"
            pr.actioned_at = pr.actioned_at or pr.created_at
            pr.actioned_by = "auto-close"
            closed.append(pr)
            report.append({"action": "closed", "barcode": d.barcode, "stage": _stage(d),
                           "part": pr.part_name, "request_type": pr.request_type,
                           "details": _details(pr), "old": "handed_over", "new": "received"})
        if closed:
            await audit(db, action="PART_AUTO_VERIFIED_BACKFILL", table_name="part_requests",
                        record_id=f"bulk:{len(closed)}",
                        new_value={"part_request_ids": [str(p.id) for p in closed]},
                        notes="Backfill: open verify requests closed (tags not in L1/L2 or L3/L4)")
        await db.flush()

        # ── Step 2: backfill RAM / Hard Drive onto the device ────────────────
        prs = (await db.execute(
            select(PartRequest)
            .where(PartRequest.status == "received")
            .order_by(PartRequest.actioned_at.asc().nulls_last(), PartRequest.created_at.asc())
        )).scalars().all()
        # RAM / Hard Drive only — judged by the live Part Master row too, so a
        # request raised under a Part Master name (category RAM / SSD / HDD)
        # still counts.
        sps = await spare_parts_for(db, prs)
        prs = [p for p in prs if effective_label(p, sps.get(p.part_id)) in CAPACITY_PARTS]
        dev_ids = list({p.device_id for p in prs})
        passes, sales = await _events(db, dev_ids) if dev_ids else ({}, {})
        already = await _applied(db, dev_ids) if dev_ids else {}
        devices = {d.id: d for d in (await db.execute(
            select(Device).where(Device.id.in_(dev_ids)))).scalars().all()} if dev_ids else {}

        per_device: dict = {}
        counts = {"spec_updated": 0, "spec_already_matched": 0, "needs_review": 0,
                  "unreadable_capacity": 0}
        for r in prs:
            if str(r.id) in already:
                continue
            did = str(r.device_id)
            state, _p, _s = classify(r.actioned_at or r.created_at,
                                     passes.get(did, []), sales.get(did, []))
            if state == "pending":
                continue                    # the next Final QC pass will apply it
            d = devices[r.device_id]
            sp = sps.get(r.part_id)
            label = effective_label(r, sp)
            gb, _ptype = capacity_and_type(r, sp)
            rtype = (r.request_type or "new").lower()
            row = {"barcode": d.barcode, "stage": _stage(d), "part": label,
                   "request_type": rtype, "details": live_details(r, sp)}
            current_gb = d.ram_gb if label == "RAM" else d.storage_gb
            current_txt = _ram_text(d) if label == "RAM" else _hdd_text(d)
            if not gb:
                counts["unreadable_capacity"] += 1
                report.append({**row, "action": "unreadable_capacity", "old": current_txt, "new": ""})
                continue
            if rtype == "new" and current_gb:
                counts["needs_review"] += 1
                report.append({**row, "action": "needs_review", "old": current_txt,
                               "new": f"? (+{_gb_label(gb)})"})
                continue
            if current_gb == gb:
                counts["spec_already_matched"] += 1
                entry = {"part_request_id": str(r.id), "part": label,
                         "request_type": rtype, "old": current_txt, "new": current_txt}
                report.append({**row, "action": "spec_already_matched", "old": current_txt, "new": current_txt})
            else:
                old, new = apply_part_to_device(d, r, sp)
                counts["spec_updated"] += 1
                entry = {"part_request_id": str(r.id), "part": label,
                         "request_type": rtype, "old": old, "new": new}
                report.append({**row, "action": "spec_updated", "old": old, "new": new})
            per_device.setdefault(r.device_id, []).append(entry)

        for dev_id, applied in per_device.items():
            d = devices[dev_id]
            await audit(db, action=AUDIT_ACTION, table_name="devices", record_id=str(d.id),
                        old_value={"barcode": d.barcode,
                                   "values": {a["part"]: a["old"] for a in applied}},
                        new_value={"barcode": d.barcode, "applied": applied, "backfill": True},
                        notes="Backfill — RAM/Hard Drive from part changes that passed Final QC "
                              "before this was automatic")

        # ── Report ───────────────────────────────────────────────────────────
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"changed_parts_backfill_{'APPLIED' if apply else 'DRYRUN'}_"
                                     f"{datetime.now():%Y%m%d_%H%M%S}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["action", "barcode", "stage", "part",
                                              "request_type", "details", "old", "new"])
            w.writeheader()
            w.writerows(report)

        print("=" * 64)
        print("CHANGED PARTS BACKFILL —", "APPLYING" if apply else "DRY RUN (nothing saved)")
        print("=" * 64)
        print(f"Step 1  open verify requests found        : {len(open_rows)}")
        print(f"        closed (marked verified)          : {len(closed)}")
        print(f"        kept open (tag in L1/L2 or L3/L4) : {kept}")
        print(f"Step 2  RAM/HDD written onto device       : {counts['spec_updated']}")
        print(f"        device already showed new value   : {counts['spec_already_matched']}")
        print(f"        needs review (New part, device    : {counts['needs_review']}  — not changed")
        print(f"        already had RAM/HDD)")
        print(f"        capacity unreadable (skipped)     : {counts['unreadable_capacity']}")
        print(f"        devices touched                   : {len(per_device)}")
        print(f"Report CSV: {path}")
        sample = [r for r in report if r["action"] in ("spec_updated", "needs_review")][:15]
        if sample:
            print("\nSample:")
            for r in sample:
                print(f"  {r['action']:<14} {r['barcode']:<20} {r['part']:<10} {r['old']} -> {r['new']}")

        if apply:
            await db.commit()
            print("\nAPPLIED and committed.")
        else:
            await db.rollback()
            print("\nDry run only — rolled back. Re-run with --apply to save.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="save the changes (default: dry run)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "backups"), help="folder for the CSV report")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.apply, args.out)))
