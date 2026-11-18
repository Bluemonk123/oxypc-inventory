"""All Inventory export (2026-09-14): "Floor" column replaced with
"Location ID", resolved the same way /devices/api/brief already resolves a
device's location (latest DeviceLocationLog entry, falling back to the
legacy warehouse/floor free-text fields)."""
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


def _seed_device(barcode, floor):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.iqc, floor="{floor}")
        db.add(dev)
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

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            dev.is_active = False
        await db.commit()

asyncio.run(main())
""")


def test_export_header_has_location_id_not_floor(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITEXPLOC{suffix}"
    _seed_device(barcode, floor="Zone A")
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
        assert "Location ID" in header, header
        assert "Floor" not in header, header

        data_rows = {row[0]: row for row in rows[1:] if row}
        assert barcode in data_rows
        row = data_rows[barcode]
        loc_idx = header.index("Location ID")
        # No DeviceLocationLog entry was seeded, so this falls back to the
        # legacy device.floor free-text field ("Zone A") — proving the
        # fallback chain still surfaces a value rather than going blank.
        assert row[loc_idx] == "Zone A", row[loc_idx]
    finally:
        _cleanup(barcode)
