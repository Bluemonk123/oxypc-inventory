"""All Inventory export (GET /devices/export, routers/devices.py
_export_rows, 2026-09-01 fix):

_iqc_cols() returned 21 blank placeholders for a device with NO IQC
inspection record yet, but 20 real values for a device that HAS one —
_EXPORT_HEADER only has 20 IQC-section columns. Since CSV columns are
positional, that one extra blank column silently shifted every later
column (cosmetics, Device Price, Grade, Stage, ...) one position right for
every tag missing an IQC record — a device just added and not yet
inspected, not a rare case. Fixed the placeholder count, and added a
row-length assertion so any future column-mapping drift fails loudly
instead of exporting misaligned data.
"""
import csv
import io
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def _seed_device(barcode, with_iqc):
    iqc_block = f"""
        db.add(IQCInspection(device_id=dev.id, power_on="Yes", status="Working"))""" if with_iqc else ""
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.iqc_inspection import IQCInspection

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.iqc)
        db.add(dev)
        await db.flush()
        {iqc_block}
        await db.commit()

asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.iqc_inspection import IQCInspection

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            iqc = (await db.execute(select(IQCInspection).where(
                IQCInspection.device_id == dev.id))).scalar_one_or_none()
            if iqc:
                await db.delete(iqc)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_export_rows_stay_column_aligned_with_and_without_iqc(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_no_iqc = f"ITEXPNOIQC{suffix}"
    barcode_with_iqc = f"ITEXPIQC{suffix}"
    _seed_device(barcode_no_iqc, with_iqc=False)
    _seed_device(barcode_with_iqc, with_iqc=True)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        r = app_client.get(f"/devices/export?q={suffix}", follow_redirects=True)
        assert r.status_code == 200, r.text[:400]

        # Excel since 2026-11-18 (coloured headers); read it back row by row.
        from openpyxl import load_workbook
        _ws = load_workbook(io.BytesIO(r.content), read_only=True).active
        reader = ([("" if c is None else str(c)) for c in row] for row in _ws.iter_rows(values_only=True))
        rows = list(reader)
        header = rows[0]
        header_len = len(header)
        assert "Grade" in header
        grade_idx = header.index("Grade")

        data_rows = {row[0]: row for row in rows[1:] if row}
        assert barcode_no_iqc in data_rows
        assert barcode_with_iqc in data_rows

        for barcode in (barcode_no_iqc, barcode_with_iqc):
            row = data_rows[barcode]
            assert len(row) == header_len, (
                f"{barcode}: row has {len(row)} columns, header has {header_len} — column shift regression."
            )
            # Grade lands in the SAME column position for both — the crux of
            # the bug: a device missing an IQC record used to push Grade (and
            # everything after it) one column to the right.
            assert row[grade_idx] in ("A", "B", "C", "S", "", "scrap"), (
                f"{barcode}: unexpected value {row[grade_idx]!r} in the Grade column — likely shifted."
            )
    finally:
        _cleanup(barcode_no_iqc)
        _cleanup(barcode_with_iqc)
