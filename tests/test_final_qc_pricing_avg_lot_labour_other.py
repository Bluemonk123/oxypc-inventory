"""Final QC's Repair History Pricing section (2026-09-14):

- Current Unit Price falls back to Avg Lot Price (lot.buying_price / lot.qty
  — the same computation IQC intake already uses to seed Device.device_price
  in the first place) when a device has no device_price of its own, instead
  of showing a bare Rs0.00.
- New Labour Price / Other Price rows read Admin -> Cost Config's Labour
  Rate (repair_labour_rate) and Other Rates (cosmetic_rate, relabelled from
  "Cosmetic Rework" — same underlying key, every other reader unaffected).
"""
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


def _seed(barcode, device_price, lot_buying_price, lot_qty):
    device_price_line = "None" if device_price is None else str(device_price)
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from utils.timezone import app_now

async def main():
    async with AsyncSessionLocal() as db:
        lot = Lot(lot_number="ITFQCLOT{barcode}", supplier_name="Test Supplier",
                  buying_price={lot_buying_price}, qty={lot_qty}, purchase_date=app_now())
        db.add(lot)
        await db.flush()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.final_qc, device_price={device_price_line})
        db.add(dev)
        await db.commit()

asyncio.run(main())
""")


def _set_cost_config(key, value):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.cost_config import CostConfig

async def main():
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(CostConfig).where(CostConfig.key == "{key}"))).scalar_one_or_none()
        if row:
            row.value = {value}
        else:
            db.add(CostConfig(key="{key}", value={value}))
        await db.commit()

asyncio.run(main())
""")


def _cleanup(barcode):
    # Deactivates the device only — the throwaway Lot row it points to is
    # left in place (Lot has no is_active flag, and hard-deleting risks an
    # FK conflict if anything else touched it during the test run), same
    # convention as every other ITEST-prefixed seed row in this suite.
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


def test_current_price_falls_back_to_avg_lot_price_when_unset(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITFQCAVG{suffix}"
    # buying_price 4000 / qty 2 = avg 2000.00
    _seed(barcode, device_price=None, lot_buying_price=4000, lot_qty=2)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        assert barcode in html, "seeded device should appear on Final QC"

        row = html.split(f'href="/devices/{barcode}"', 1)[1][:20000]
        assert "Avg Lot Price" in row
        assert "2000.00" in row
    finally:
        _cleanup(barcode)


def test_current_price_uses_device_price_when_set_no_avg_lot_label(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITFQCSET{suffix}"
    _seed(barcode, device_price=3333, lot_buying_price=4000, lot_qty=2)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        assert barcode in html

        row = html.split(f'href="/devices/{barcode}"', 1)[1][:20000]
        assert "3333.00" in row
        assert "Avg Lot Price" not in row.split("Current Unit Price", 1)[1][:200]
    finally:
        _cleanup(barcode)


def test_labour_and_other_price_rows_read_cost_config(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITFQCCFG{suffix}"
    _seed(barcode, device_price=1000, lot_buying_price=2000, lot_qty=1)
    _set_cost_config("repair_labour_rate", "175.00")
    _set_cost_config("packing_rate", "65.00")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        assert barcode in html

        row = html.split(f'href="/devices/{barcode}"', 1)[1][:20000]
        # 2026-10-29: "Labour Price" (flat Labour Rate) became "Repair Cost" —
        # Labor Rate by device type + Packing (packing_rate, "On Tags" by
        # default) + Freight. services/repair_cost.py
        assert "Repair Cost" in row and "Packing ₹65" in row
        assert "Labour Price" not in row and "Other Price" not in row
    finally:
        _cleanup(barcode)
        # Restore the seeded defaults so other tests/pages aren't affected.
        _set_cost_config("repair_labour_rate", "150.00")
        _set_cost_config("packing_rate", "0.00")


def test_cost_config_page_shows_renamed_other_rates_label(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/admin/cost-config", follow_redirects=True).text
    # 2026-10-29: "Other Rates per Device" is now "Packing Charges".
    assert "Packing Charges" in html and "Other Rates per Device" not in html
    assert "Cosmetic Rework" not in html
    # Underlying key is unchanged — every other CostConfig reader still works.
    assert 'name="packing_rate"' in html and 'name="cosmetic_rate"' not in html
