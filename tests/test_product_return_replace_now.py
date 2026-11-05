"""Product Return — Replace Now (2026-09-14):

* Internal Tags tab's Return Type field is gone.
* A "Replace Now" = Yes + a Ready to Sale replacement Tag Number marks that
  replacement device sold at ₹0, using the same customer details as the
  original (Device Identity) sale, and tags it with Device.replaced =
  "Replaced with <identity barcode>" — which the Returns list renders as a
  badge on a synthesized row (no Return record exists for a replacement,
  it was never itself "returned").
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


def _seed_identity_device(barcode):
    """A device with a prior Sale (so Process Return has something to return)."""
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Sale

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.sold)
        db.add(dev)
        await db.flush()
        sale_number = "ITSID" + uuid.uuid4().hex[:12].upper()  # fits VARCHAR(20)
        sale = Sale(sale_number=sale_number, device_id=dev.id, sale_price=5000,
                    customer_name="Test Customer", customer_phone="9999999999",
                    customer_address="Test Address")
        db.add(sale)
        await db.commit()

asyncio.run(main())
""")


def _seed_replacement_device(barcode, stage="ready_to_sale"):
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
                     current_stage=DeviceStage.{stage})
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


def _read_device(barcode):
    """Print current_stage.value,replaced for a device."""
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        print(f"{{dev.current_stage.value}}|||{{dev.replaced or ''}}")

asyncio.run(main())
""")


def _read_replacement_sale(barcode):
    """Print sale_price,customer_name,customer_phone for the device's Sale."""
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Sale

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        sale = (await db.execute(
            select(Sale).where(Sale.device_id == dev.id).order_by(Sale.sold_at.desc()).limit(1)
        )).scalars().first()
        print(sale.sale_price, "|", sale.customer_name, "|", sale.customer_phone)

asyncio.run(main())
""")


def test_return_form_no_longer_shows_return_type_field(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/returns/new").text
    assert 'name="return_type"' not in html
    assert 'id="replaceNowYes"' in html
    assert 'id="replaceNowBox"' in html


def test_replace_now_sells_replacement_at_zero_with_same_customer(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    identity_barcode = f"ITRTNID{suffix}"
    repl_barcode = f"ITRTNRP{suffix}"
    _seed_identity_device(identity_barcode)
    _seed_replacement_device(repl_barcode, stage="ready_to_sale")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or ""

        r = app_client.post(
            "/returns/new",
            data={"complaint_text": "Test complaint", 
                "csrf_token": csrf,
                "barcode": identity_barcode,
                "reason": "Defective",
                "condition_on_return": "As sold",
                "action_taken": "restock",
                "replace_now": "yes",
                "replace_tag": repl_barcode,
            },
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:400]
        assert "success" in r.headers.get("location", ""), r.headers.get("location")

        stage, replaced = _read_device(repl_barcode).split("|||")
        assert stage == "sold", f"replacement device should be sold, is {stage}"
        assert replaced == f"Replaced with {identity_barcode}", replaced

        price, cust_name, cust_phone = _read_replacement_sale(repl_barcode).split(" | ")
        assert price == "0.00" or price == "0", price
        assert cust_name == "Test Customer"
        assert cust_phone == "9999999999"

        # Returns list shows the replacement row with its badge.
        page = app_client.get("/returns").text
        assert repl_barcode in page
        assert f"Replaced with {identity_barcode}" in page
    finally:
        _cleanup(identity_barcode)
        _cleanup(repl_barcode)


def test_replace_now_rejects_a_device_not_in_ready_to_sale(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    identity_barcode = f"ITRTNID2{suffix}"
    repl_barcode = f"ITRTNRP2{suffix}"
    _seed_identity_device(identity_barcode)
    _seed_replacement_device(repl_barcode, stage="iqc")  # not Ready to Sale
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or ""

        r = app_client.post(
            "/returns/new",
            data={"complaint_text": "Test complaint", 
                "csrf_token": csrf,
                "barcode": identity_barcode,
                "reason": "Defective",
                "condition_on_return": "As sold",
                "action_taken": "restock",
                "replace_now": "yes",
                "replace_tag": repl_barcode,
            },
            follow_redirects=False,
        )
        assert r.status_code == 200, r.text[:400]
        assert "not in Ready to Sale stage" in r.text

        # Nothing should have been created.
        stage, replaced = _read_device(repl_barcode).split("|||")
        assert stage == "iqc"
        assert replaced == ""
    finally:
        _cleanup(identity_barcode)
        _cleanup(repl_barcode)
