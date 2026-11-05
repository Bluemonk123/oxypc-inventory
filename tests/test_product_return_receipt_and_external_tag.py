"""Product Return — Receipt column + New page's Internal/External tabs
(2026-09-02):

- /returns list gained a per-row "Receipt" link opening
  GET /returns/{return_id}/receipt (standalone printable page) with Entity,
  Customer Name/Phone/Email/Address, Reason, Condition, Tag Number, Serial
  Number (reuses the existing Return.serial_captured column), Make, Model,
  Quantity (always 1), and Paid Repair (reuses Return.refund_amount).
- /returns/new is now two tabs: "Internal Tags" (the existing tag-in-system
  flow, unchanged) and "External Tag" (new — POST /returns/new/external),
  for a walk-in item with no prior Sale in this system. A placeholder Sale
  is created (sale_price=0) since Return.sale_id is NOT NULL, and the
  Device is created (or reused) and marked return_status=True, which is the
  only thing Inventory Manager's Return Stock table requires — so it
  surfaces there automatically.
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


def _seed_internal_return(barcode):
    """A device + sale + return, mirroring the existing Internal Tags flow,
    to exercise the Receipt page against a real row."""
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Sale, Return

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     entity="OxyPC Computers", current_stage=DeviceStage.sold)
        db.add(dev)
        await db.flush()
        sale = Sale(sale_number="SALERCPT{barcode[-6:]}", device_id=dev.id, sale_price=15000,
                    customer_name="Receipt Customer", customer_phone="9991112222",
                    customer_address="123 Test Street")
        db.add(sale)
        await db.flush()
        ret = Return(sale_id=sale.id, device_id=dev.id, reason="Not working",
                     condition_on_return="Minor damage", serial_captured="{barcode}",
                     refund_amount=1500, approval_status="pending",
                     customer_email="receipt@example.com")
        db.add(ret)
        await db.commit()
        print(ret.id)

asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Sale, Return

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for r in (await db.execute(select(Return).where(Return.device_id == dev.id))).scalars().all():
                await db.delete(r)
            for s in (await db.execute(select(Sale).where(Sale.device_id == dev.id))).scalars().all():
                await db.delete(s)
            from sqlalchemy import text as _t
            for t in ("iqc_inspections", "stage_movements", "device_location_logs"):
                await db.execute(_t(f"delete from {{t}} where device_id = :d"), {{"d": dev.id}})
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_returns_list_has_receipt_button_with_data(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITRETRCPT{suffix}"
    return_id = _seed_internal_return(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/returns", follow_redirects=True).text
        assert f'data-id="{return_id}"' in html
        assert 'class="modal fade" id="receiptModal"' in html
        assert "Receipt Customer" in html
        assert "receipt@example.com" in html
    finally:
        _cleanup(barcode)


def test_receipt_page_shows_all_requested_fields(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITRETRCPT2{suffix}"
    return_id = _seed_internal_return(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(f"/returns/{return_id}/receipt", follow_redirects=True).text
        assert "OxyPC Computers" in html          # Entity
        assert "Receipt Customer" in html          # Customer Name
        assert "9991112222" in html                # Customer Phone
        assert "receipt@example.com" in html       # Customer Email
        assert "123 Test Street" in html           # Customer Address
        assert "Not working" in html               # Reason
        assert "Minor damage" in html               # Condition
        assert barcode in html                      # Tag Number
        assert "ITestBrand" in html                 # Make
        assert "ITestModel" in html                 # Model
        assert ">1<" in html                        # Quantity
        assert "1500.00" in html                    # Paid Repair
    finally:
        _cleanup(barcode)


def test_return_form_has_internal_and_external_tabs(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/returns/new", follow_redirects=True).text
    assert "Internal Tag" in html
    assert "External Tag" in html
    assert 'action="/returns/new/external"' in html
    assert 'name="tag_number"' in html
    assert 'name="serial_number"' in html
    assert 'name="customer_email"' in html
    assert 'name="paid_repair"' in html


def test_external_tag_creates_return_and_shows_in_return_stock(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    tag_number = f"ITEXT{suffix}"
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post("/returns/new/external", data={"complaint_text": "Test complaint", 
            "csrf_token": csrf,
            "entity": "OxyPC Computers",
            "customer_name": "External Customer",
            "customer_phone": "9998887777",
            "customer_email": "external@example.com",
            "customer_address": "456 External Ave",
            "reason": "Not working",
            "condition_on_return": "Minor damage",
            "tag_number": tag_number,
            "serial_number": "SN-12345",
            "make": "ITestBrand",
            "model": "ITestModel",
            "cpu": "i5-8350U", "generation": "8th", "ram": "16 GB", "storage": "512 GB SSD",
            "quantity": "1",
            "paid_repair": "1500",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:500]

        # It's on the Product Return list.
        returns_html = app_client.get("/returns", follow_redirects=True).text
        assert tag_number in returns_html

        # And it surfaces on Inventory Manager's Return Stock table.
        stock_html = app_client.get("/stock", follow_redirects=True).text
        assert tag_number in stock_html
        assert "Not working" in stock_html

        state = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Sale

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{tag_number}"))).scalar_one()
        print(dev.return_status)
        print(dev.brand)
        print(dev.entity)
        sale = (await db.execute(select(Sale).where(Sale.device_id == dev.id))).scalar_one()
        print(sale.sale_price)
        from models.lot import Lot
        from models.iqc_inspection import IQCInspection
        print((await db.get(Lot, dev.lot_id)).lot_number)
        print(dev.current_stage.value)
        print(dev.cpu, dev.generation, dev.ram_gb, dev.storage_gb, sep="|")
        print((await db.execute(select(IQCInspection).where(IQCInspection.device_id == dev.id))).scalar_one_or_none() is not None)

asyncio.run(main())
""").splitlines()
        assert state[0] == "True"
        assert state[1] == "ITestBrand"
        assert state[2] == "OxyPC Computers"
        assert state[3] == "0.00" or state[3] == "0"
        # 2026-11-05: new External tag -> Lot CUST, IQC record, lands at Stock In
        assert state[4] == "CUST"
        assert state[5] == "stock_in"
        assert state[6] == "i5-8350U|8th|16|512"
        assert state[7] == "True"
    finally:
        _cleanup(tag_number)


def test_external_tag_requires_tag_number_reason_condition(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    r = app_client.post("/returns/new/external", data={"complaint_text": "Test complaint", 
        "csrf_token": csrf, "tag_number": "", "reason": "", "condition_on_return": "",
    }, follow_redirects=True)
    assert "required" in r.text.lower()


def test_external_tag_requires_specs(app_client, make_user):  # noqa: F811
    """2026-11-05: CPU / CPU Gen / RAM / Storage are mandatory ("0" allowed)."""
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    tag = f"ITEXT{uuid.uuid4().hex[:6]}"
    base = {"complaint_text": "Dead", "csrf_token": csrf, "entity": "OxyPC Computers", "customer_name": "X",
            "reason": "Not working", "condition_on_return": "Dead", "tag_number": tag, "quantity": "1",
            "paid_repair": "0", "cpu": "0", "generation": "0", "ram": "0", "storage": ""}
    r = app_client.post("/returns/new/external", data=base, follow_redirects=True)
    assert "CPU, CPU Gen, RAM and Storage are required" in r.text
    try:
        r = app_client.post("/returns/new/external", data={**base, "storage": "0"}, follow_redirects=False)
        assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")
    finally:
        _cleanup(tag)
