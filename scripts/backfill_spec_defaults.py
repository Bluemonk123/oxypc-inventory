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
Hard Drive value = storage_gb, else the Hard Drive text (hdd_summary)
A device with no readable RAM / Hard Drive value gets size "0".

Default is a DRY RUN: prints the summary, writes a CSV of every change it
would make to backups/, then ROLLS BACK. Nothing changes until --apply.

Production (PuTTY):
    cd /opt/oxypc
    sudo docker compose exec oxypc-app python scripts/backfill_spec_defaults.py
    sudo docker compose exec oxypc-app python scripts/backfill_spec_defaults.py --apply

Re-running is safe: filled fields are no longer blank, so they are skipped.
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


def _value_gb(gb, text) -> int | None:
    if gb:
        return int(gb)
    # a packed multi-stick string ("8GB_DDR4, 4GB_DDR4") has no single value
    if text and "," not in str(text):
        return parse_capacity_gb(text)
    return None


def _cpu_make(d) -> str:
    if "apple" in (d.brand or "").lower():
        return "Apple"
    if re.search(r"\bamd\b|ryzen", (d.cpu or "").lower()):
        return "AMD"
    return "Intel"


async def run(apply: bool, out_dir: str) -> int:
    report: list[dict] = []
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        devices = (await db.execute(select(Device))).scalars().all()
        touched = set()

        def change(d, field, old, new, note=""):
            setattr(d, field, new)
            counts[field] += 1
            touched.add(d.id)
            report.append({"action": "filled", "barcode": d.barcode, "brand": d.brand or "",
                           "field": field, "old": old or "", "new": new, "note": note})

        for d in devices:
            # ── RAM ──────────────────────────────────────────────────────────
            old_cnt = d.total_ram_count
            if _blank(d.total_ram_count):
                change(d, "total_ram_count", old_cnt, "1")
            if _blank(d.total_ram_size):
                v = _value_gb(d.ram_gb, d.ram_summary)
                if v:
                    n = _count(d.total_ram_count)
                    note = f"{n} x {_gb_text(v)}"
                    if n > 1:
                        counts["ram_count_gt1"] += 1
                        note += "  (existing count > 1)"
                    change(d, "total_ram_size", d.total_ram_size, _gb_text(n * v), note)
                else:
                    counts["ram_no_value"] += 1
                    change(d, "total_ram_size", d.total_ram_size, "0",
                           "device has no RAM value -> 0")
            # ── Hard Drive ───────────────────────────────────────────────────
            old_cnt = d.total_hdd_count
            if _blank(d.total_hdd_count):
                change(d, "total_hdd_count", old_cnt, "1")
            if _blank(d.total_hdd_size):
                v = _value_gb(d.storage_gb, d.hdd_summary)
                if v:
                    n = _count(d.total_hdd_count)
                    note = f"{n} x {_gb_text(v)}"
                    if n > 1:
                        counts["hdd_count_gt1"] += 1
                        note += "  (existing count > 1)"
                    change(d, "total_hdd_size", d.total_hdd_size, _gb_text(n * v), note)
                else:
                    counts["hdd_no_value"] += 1
                    change(d, "total_hdd_size", d.total_hdd_size, "0",
                           "device has no Hard Drive value -> 0")
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
        print(f"Devices that get at least one value  : {len(touched)}")
        print(f"Total RAM Count   set to 1           : {counts['total_ram_count']}")
        print(f"Total RAM Size    filled             : {counts['total_ram_size']}")
        print(f"   of which existing count > 1       : {counts['ram_count_gt1']}")
        print(f"   set to 0, no RAM value on device  : {counts['ram_no_value']}")
        print(f"Total HDD Count   set to 1           : {counts['total_hdd_count']}")
        print(f"Total HDD Size    filled             : {counts['total_hdd_size']}")
        print(f"   of which existing count > 1       : {counts['hdd_count_gt1']}")
        print(f"   set to 0, no HDD value on device  : {counts['hdd_no_value']}")
        print(f"CPU Make filled                      : {counts['cpu_make']}")
        print(f"   Intel {counts['cpu_make_Intel']}  |  Apple {counts['cpu_make_Apple']}  |  "
              f"AMD (from CPU name) {counts['cpu_make_AMD']}")
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
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.apply, args.out)))
