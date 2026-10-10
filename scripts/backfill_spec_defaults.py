"""One-time backfill of blank spec fields on devices.

Only BLANK fields are filled — anything already entered is never touched.

  Total RAM Count        blank -> "1"
  Total RAM Size         blank -> RAM Count x RAM value          (e.g. 1 x 8GB = "8GB")
  Total Hard Drive Count blank -> "1"
  Total Hard Drive Size  blank -> Hard Drive Count x Hard Drive value
  CPU Make               blank -> "Apple" if Brand is Apple, else "Intel"
                                  (the CPU name says AMD / Ryzen -> "AMD", so an
                                  AMD laptop is not mislabelled Intel)

RAM value        = ram_gb, else the RAM text (ram_summary, e.g. "8GB")
Hard Drive value = storage_gb, else the second drive (hdd_capacity_gb),
                   else the Hard Drive text (hdd_summary)
Several sticks / drives listed ("8GB, 8GB") -> they are ADDED (16GB) and a
blank count becomes the number listed. Main + second drive both set ->
size = both added, blank count -> 2.
A device with no readable RAM / Hard Drive value gets size "0", and a blank
count next to a 0 size becomes "0" (not 1).

Tags still in GRN or IQC are SKIPPED entirely — the IQC form pre-fills from
the device, so defaults there would look like inspected values.

Default is a DRY RUN: prints the summary, writes a CSV of every change it
would make to backups/, then ROLLS BACK. Nothing changes until --apply.

Production (PuTTY):
    cd /opt/oxypc
    sudo docker compose exec oxypc-app python scripts/backfill_spec_defaults.py
    sudo docker compose exec oxypc-app python scripts/backfill_spec_defaults.py --apply

Re-running is safe: filled fields are no longer blank, so they are skipped.

Undo an earlier --apply (puts every field back to what it was, using that
run's CSV; a field someone has changed since is left alone):
    python scripts/backfill_spec_defaults.py --undo backups/spec_defaults_backfill_APPLIED_....csv
    python scripts/backfill_spec_defaults.py --undo backups/... --apply
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from database import AsyncSessionLocal  # noqa: E402
from models.device import Device  # noqa: E402
from services.audit_engine import audit  # noqa: E402
from services.parts_changed import _gb_text, parse_capacity_gb  # noqa: E402


def _blank(v) -> bool:
    return v is None or str(v).strip() in ("", "-", "—", "None", "none", "null", "N/A", "n/a", "NA")


def _count(v) -> int:
    """Existing count as int (blank / unreadable -> 1)."""
    m = re.search(r"\d+", str(v or ""))
    n = int(m.group()) if m else 1
    return n if n > 0 else 1


SKIP_STAGES = {"grn", "iqc"}
MAX_COUNT = 4      # more sticks / drives than this = a data-entry slip, not a count


def _from_text(text):
    """(gb, pieces) from a RAM/HDD text. Packed lists are summed;
    pieces is the number listed (None for a single value)."""
    if not text:
        return None, None
    text = str(text).replace("_", " ")     # "16GB_DDR4" -> "16GB DDR4" (word boundary)
    parts = [p for p in re.split(r"[,;+/]|\s&\s", str(text)) if p.strip()]
    if len(parts) > 1:
        vals = [parse_capacity_gb(p, need_unit=True) for p in parts]
        vals = [v for v in vals if v]
        return (sum(vals), len(vals)) if vals else (None, None)
    return parse_capacity_gb(text), None


def _is_zero(v) -> bool:
    """Size text that means nothing installed: "0", "0GB", "0 GB"."""
    return bool(re.fullmatch(r"\s*0+(\.0+)?\s*(gb|tb)?\s*", str(v or ""), re.I))


def _ram_value(d):
    if d.ram_gb:
        return int(d.ram_gb), None
    return _from_text(d.ram_summary)


def _hdd_value(d):
    if d.storage_gb and d.hdd_capacity_gb:
        return int(d.storage_gb) + int(d.hdd_capacity_gb), 2
    if d.storage_gb:
        return int(d.storage_gb), None
    if d.hdd_capacity_gb:
        return int(d.hdd_capacity_gb), None
    return _from_text(d.hdd_summary)


def _cpu_make(d) -> str:
    if "apple" in (d.brand or "").lower():
        return "Apple"
    if re.search(r"\bamd\b|ryzen", (d.cpu or "").lower()):
        return "AMD"
    return "Intel"


UNDO_FIELDS = {"total_ram_count", "total_ram_size", "total_hdd_count", "total_hdd_size", "cpu_make"}


async def undo(csv_path: str, apply: bool) -> int:
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("action") == "filled" and r.get("field") in UNDO_FIELDS]
    by_bc: dict = {}
    for r in rows:
        by_bc.setdefault(r["barcode"], []).append(r)
    reverted, changed_since, missing = 0, 0, 0
    async with AsyncSessionLocal() as db:
        bcs = list(by_bc)
        devs = {}
        for i in range(0, len(bcs), 5000):
            for d in (await db.execute(select(Device).where(Device.barcode.in_(bcs[i:i + 5000])))).scalars():
                devs[d.barcode] = d
        for bc, rs in by_bc.items():
            d = devs.get(bc)
            if not d:
                missing += len(rs)
                continue
            for r in rs:
                if (getattr(d, r["field"]) or "") == r["new"]:
                    setattr(d, r["field"], r["old"] or None)
                    reverted += 1
                else:
                    changed_since += 1
        if reverted:
            await audit(db, action="DEVICE_SPEC_DEFAULTS_UNDO", table_name="devices",
                        record_id=f"bulk:{reverted}",
                        new_value={"undo_of": os.path.basename(csv_path), "fields_reverted": reverted},
                        notes="Undo of spec-defaults backfill")
        print("=" * 64)
        print("SPEC DEFAULTS UNDO —", "APPLYING" if apply else "DRY RUN (nothing saved)")
        print("=" * 64)
        print(f"Fields in CSV                        : {len(rows)}")
        print(f"Put back to previous value           : {reverted}")
        print(f"Left alone (changed since)           : {changed_since}")
        print(f"Tag no longer exists                 : {missing}")
        if apply:
            await db.commit()
            print("\nAPPLIED and committed.")
        else:
            await db.rollback()
            print("\nDry run only — rolled back. Re-run with --apply to save.")
    return 0


async def run(apply: bool, out_dir: str) -> int:
    report: list[dict] = []
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        devices = (await db.execute(select(Device))).scalars().all()
        touched = set()
        zero_stages = {"ram": Counter(), "hdd": Counter()}

        def change(d, field, old, new, note=""):
            setattr(d, field, new)
            counts[field] += 1
            touched.add(d.id)
            report.append({"action": "filled", "barcode": d.barcode, "brand": d.brand or "",
                           "field": field, "old": old or "", "new": new, "note": note})

        for d in devices:
            stage = getattr(d.current_stage, "value", str(d.current_stage or ""))
            if stage in SKIP_STAGES:
                counts["skipped_grn_iqc"] += 1
                continue
            for part, label, (v, pieces) in (("ram", "RAM", _ram_value(d)),
                                             ("hdd", "Hard Drive", _hdd_value(d))):
                cnt_f, size_f = f"total_{part}_count", f"total_{part}_size"
                count_blank = _blank(getattr(d, cnt_f))
                # size first — a blank count follows the size (0 size -> 0 count)
                if _blank(getattr(d, size_f)):
                    if v and pieces:
                        counts[f"{part}_summed"] += 1
                        change(d, size_f, getattr(d, size_f), _gb_text(v),
                               f"{pieces} added = {_gb_text(v)}")
                    elif v and _count(getattr(d, cnt_f)) > MAX_COUNT:
                        # "16" in the count field next to 16GB RAM = the size typed
                        # into the count box; multiplying would give 256GB
                        counts[f"{part}_count_suspect"] += 1
                        change(d, size_f, getattr(d, size_f), _gb_text(v),
                               f"count {getattr(d, cnt_f)!r} looks wrong -> not multiplied")
                    elif v:
                        n = _count(getattr(d, cnt_f))
                        note = f"{n} x {_gb_text(v)}"
                        if n > 1:
                            counts[f"{part}_count_gt1"] += 1
                            note += "  (existing count > 1)"
                        change(d, size_f, getattr(d, size_f), _gb_text(n * v), note)
                    else:
                        counts[f"{part}_no_value"] += 1
                        zero_stages[part][stage] += 1
                        change(d, size_f, getattr(d, size_f), "0",
                               f"device has no {label} value -> 0")
                if count_blank:
                    if _is_zero(getattr(d, size_f)):
                        counts[f"{part}_count_zero"] += 1
                        change(d, cnt_f, getattr(d, cnt_f), "0", "size is 0 -> count 0")
                    elif v and pieces:
                        change(d, cnt_f, getattr(d, cnt_f), str(pieces), f"{pieces} listed")
                    else:
                        change(d, cnt_f, getattr(d, cnt_f), "1", "default")
            # ── CPU Make ─────────────────────────────────────────────────────
            if _blank(d.cpu_make):
                mk = _cpu_make(d)
                counts[f"cpu_make_{mk}"] += 1
                change(d, "cpu_make", d.cpu_make, mk,
                       "from CPU name" if mk == "AMD" else ("Brand is Apple" if mk == "Apple" else "default"))

        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"spec_defaults_backfill_{'APPLIED' if apply else 'DRYRUN'}_"
                                     f"{datetime.now():%Y%m%d_%H%M%S}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["action", "barcode", "brand", "field", "old", "new", "note"])
            w.writeheader()
            w.writerows(report)

        if touched:
            await audit(db, action="DEVICE_SPEC_DEFAULTS_BACKFILL", table_name="devices",
                        record_id=f"bulk:{len(touched)}",
                        new_value={"devices": len(touched),
                                   "fields": {k: counts[k] for k in ("total_ram_count", "total_ram_size",
                                              "total_hdd_count", "total_hdd_size", "cpu_make")},
                                   "report_csv": os.path.basename(path)},
                        notes="Backfill: blank RAM/HDD count+size and CPU Make filled with defaults")

        print("=" * 64)
        print("SPEC DEFAULTS BACKFILL —", "APPLYING" if apply else "DRY RUN (nothing saved)")
        print("=" * 64)
        print(f"Devices scanned                      : {len(devices)}")
        print(f"Skipped, tag still in GRN / IQC      : {counts['skipped_grn_iqc']}")
        print(f"Devices that get at least one value  : {len(touched)}")
        print(f"Total RAM Count   filled (1 / 0 / #listed): {counts['total_ram_count']}")
        print(f"Total RAM Size    filled             : {counts['total_ram_size']}")
        print(f"   of which existing count > 1       : {counts['ram_count_gt1']}")
        print(f"   of which several sticks added     : {counts['ram_summed']}")
        print(f"   count looked wrong, not multiplied: {counts['ram_count_suspect']}")
        print(f"RAM Count set to 0 (size is 0)       : {counts['ram_count_zero']}")
        print(f"   set to 0, no RAM value on device  : {counts['ram_no_value']}")
        print(f"Total HDD Count   filled (1 / 0 / #listed): {counts['total_hdd_count']}")
        print(f"Total HDD Size    filled             : {counts['total_hdd_size']}")
        print(f"   of which existing count > 1       : {counts['hdd_count_gt1']}")
        print(f"   of which several drives added     : {counts['hdd_summed']}")
        print(f"   count looked wrong, not multiplied: {counts['hdd_count_suspect']}")
        print(f"HDD Count set to 0 (size is 0)       : {counts['hdd_count_zero']}")
        print(f"   set to 0, no HDD value on device  : {counts['hdd_no_value']}")
        print(f"CPU Make filled                      : {counts['cpu_make']}")
        print(f"   Intel {counts['cpu_make_Intel']}  |  Apple {counts['cpu_make_Apple']}  |  "
              f"AMD (from CPU name) {counts['cpu_make_AMD']}")
        for part, label in (("ram", "RAM"), ("hdd", "HDD")):
            if zero_stages[part]:
                top = ", ".join(f"{k} {v}" for k, v in zero_stages[part].most_common(8))
                print(f"{label} size 0 by stage: {top}")
        print(f"Report CSV: {path}")
        sample = [r for r in report if r["action"] == "filled" and r["field"].endswith("size")][:10]
        if sample:
            print("\nSample:")
            for r in sample:
                print(f"  {r['barcode']:<20} {r['field']:<15} -> {r['new']:<8} ({r['note']})")

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
    ap.add_argument("--undo", metavar="CSV", help="revert an earlier --apply using its CSV report")
    args = ap.parse_args()
    if args.undo:
        sys.exit(asyncio.run(undo(args.undo, args.apply)))
    sys.exit(asyncio.run(run(args.apply, args.out)))
