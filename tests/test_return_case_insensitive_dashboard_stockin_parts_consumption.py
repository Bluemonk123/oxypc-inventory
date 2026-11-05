"""2026-09-21 batch:
 - /returns/new (Internal Tags tab): the "Check" and "Replace With" searchboxes
   already matched barcodes case-insensitively (Device.barcode.ilike()), but
   the form's own POST handler (process_return) re-queried both the returned
   device and the replacement device with an exact `Device.barcode ==` /
   `== rtag` comparison — so a tag typed/scanned in different case than
   stored (this codebase's barcodes are inconsistently cased) passed the
   searchbox check but then failed at submit with "Device ... not found" /
   "Replacement device ... not found.", even though the tag is real and the
   replacement is genuinely sitting in Ready to Sale. Fixed to match on
   func.upper(Device.barcode) == <value>.upper(), same fix pattern already
   used by the CSV bulk tag upload in this same file.
 - Dashboard Stage Pipeline gains a "Stock In" tile (DeviceStage.stock_in),
   slotted right after IQC per the row's own declared DeviceStage order. The
   stale "L3/L4 shown above is the subset of L1 awaiting L3/L4." footnote
   (obsolete since request_l3l4() now moves devices to a real DeviceStage.l3)
   is removed.
 - Spare Parts Master's "Parts Consumption" tab gains Tag Make / Tag Model
   columns (Device.brand / Device.model) right after Tag Number — covers the
   on-page table and its client-side Export (spExport scrapes the same
   <table>, so no separate export code path exists to update).
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


# ─────────────────────────── AREA 1: /returns/new ────────────────────────────

def _seed_sold_device_and_replacement(sold_barcode, replace_barcode, sale_number):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Sale
from utils.timezone import app_now

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        sold = Device(barcode="{sold_barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                      current_stage=DeviceStage.sold)
        repl = Device(barcode="{replace_barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel2",
                      current_stage=DeviceStage.ready_to_sale)
        db.add(sold)
        db.add(repl)
        await db.flush()
        sale = Sale(sale_number="{sale_number}", device_id=sold.id, sale_price=10000,
                    customer_name="ITest Customer", sold_by="itest", sold_at=app_now())
        db.add(sale)
        await db.commit()

asyncio.run(main())
""")


def _cleanup_return_devices(sold_barcode, replace_barcode, sale_number):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement
from models.sales import Sale, Return

async def main():
    async with AsyncSessionLocal() as db:
        # Sale.device_id is NOT NULL with no cascade — delete Sale/Return rows
        # that reference a device BEFORE deleting the device itself, or the
        # ORM tries to null the FK out first and trips the NOT NULL constraint.
        for bc in ("{sold_barcode}", "{replace_barcode}"):
            dev = (await db.execute(select(Device).where(Device.barcode == bc))).scalar_one_or_none()
            if dev:
                for ret in (await db.execute(select(Return).where(
                        Return.device_id == dev.id))).scalars().all():
                    await db.delete(ret)
                for sale in (await db.execute(select(Sale).where(
                        Sale.device_id == dev.id))).scalars().all():
                    await db.delete(sale)
                for m in (await db.execute(select(StageMovement).where(
                        StageMovement.device_id == dev.id))).scalars().all():
                    await db.delete(m)
                await db.flush()
                await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_process_return_matches_barcode_case_insensitively(app_client, make_user):  # noqa: F811
    """The stored barcode is lowercase; POSTing an uppercase variant (as the
    Device Identity searchbox would resolve it to, via .ilike()) must not
    bounce with 'Device ... not found'."""
    suffix = uuid.uuid4().hex[:6]
    sold_barcode = f"itretcase{suffix}"
    replace_barcode = f"ITRETREPL{suffix}"
    sale_number = f"ITSALE{suffix}"
    _seed_sold_device_and_replacement(sold_barcode, replace_barcode, sale_number)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post("/returns/new", data={"complaint_text": "Test complaint", 
            "csrf_token": csrf,
            "barcode": sold_barcode.upper(),  # typed in the OPPOSITE case of stored
            "reason": "Physical damage",
            "condition_on_return": "Minor damage",
            "replace_now": "no",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:800]
        assert "success" in r.headers.get("location", "")
    finally:
        _cleanup_return_devices(sold_barcode, replace_barcode, sale_number)


def test_process_return_replacement_tag_matches_case_insensitively(app_client, make_user):  # noqa: F811
    """Replace Now = yes with a replacement tag typed in a different case than
    stored must not bounce with 'Replacement device ... not found.' — it's
    genuinely sitting in Ready to Sale."""
    suffix = uuid.uuid4().hex[:6]
    sold_barcode = f"itretcase2{suffix}"
    replace_barcode = f"itretrepl2{suffix}"  # stored lowercase
    sale_number = f"ITSALE2{suffix}"
    _seed_sold_device_and_replacement(sold_barcode, replace_barcode, sale_number)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post("/returns/new", data={"complaint_text": "Test complaint", 
            "csrf_token": csrf,
            "barcode": sold_barcode,
            "reason": "Physical damage",
            "condition_on_return": "Minor damage",
            "replace_now": "yes",
            "replace_tag": replace_barcode.upper(),  # opposite case of stored
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:800]
        assert "success" in r.headers.get("location", "")

        state = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{replace_barcode}"))).scalar_one()
        print(dev.current_stage.value)

asyncio.run(main())
""")
        assert state == "sold", "replacement device should have been sold as part of the return"
    finally:
        _cleanup_return_devices(sold_barcode, replace_barcode, sale_number)


def test_devices_api_brief_shows_current_stage(app_client, make_user):  # noqa: F811
    """The searchbox JSON payload must carry the current stage label so the
    Internal Tags form can show it in both the success and error/brief
    messages (this is device.stage_label, already returned as 'status')."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITBRIEFSTAGE{suffix}"
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
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="X", model="Y",
                     current_stage=DeviceStage.ready_to_sale)
        db.add(dev)
        await db.commit()

asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        r = app_client.get(f"/devices/api/brief?barcode={barcode.lower()}")
        assert r.status_code == 200
        d = r.json()
        assert d["found"] is True
        assert d["status"] == "Ready to Sale"
    finally:
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
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_return_form_shows_current_stage_in_both_searchboxes(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/returns/new", follow_redirects=True).text
    assert 'id="br_stage"' in html
    assert 'id="rp_stage"' in html
    assert "Current Stage" in html
    assert "Current stage" in html  # inline message text


# ────────────────────────── AREA 2: Dashboard tiles ───────────────────────────

def test_dashboard_has_stock_in_tile_after_iqc(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/dashboard", follow_redirects=True).text
    labels_in_order = ["GRN", "IQC", "Stock In", "L1/L2", "L3/L4", "Production",
                        "Stress/QC", "Cosmetic", "Final QC", "Ready to Sale", "Sold"]
    positions = [html.index(f">{label}<") for label in labels_in_order]
    assert positions == sorted(positions), "Stock In tile is not positioned right after IQC"


def test_dashboard_stale_l3l4_subset_line_removed(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/dashboard", follow_redirects=True).text
    assert "subset of L1 awaiting L3/L4" not in html
    # The Returned/Scrapped/Scrap for Sale callout must still be present.
    assert "Scrap for Sale" in html


# ───────────────────────── AREA 3: Parts Consumption ──────────────────────────

def _seed_received_part_request(barcode, part_make, part_model):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.part_request import PartRequest
from utils.timezone import app_now

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="{part_make}", model="{part_model}",
                     current_stage=DeviceStage.l1)
        db.add(dev)
        await db.flush()
        pr = PartRequest(device_id=dev.id, barcode="{barcode}", stage="l1",
                         part_name="ITest Keyboard", request_type="new",
                         qty_requested=1, qty_handed_over=1, status="received",
                         actioned_at=app_now(), actioned_by="itest")
        db.add(pr)
        await db.commit()

asyncio.run(main())
""")


def _cleanup_part_request(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.part_request import PartRequest

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for pr in (await db.execute(select(PartRequest).where(
                    PartRequest.device_id == dev.id))).scalars().all():
                await db.delete(pr)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_parts_consumption_tab_has_tag_make_and_model_columns(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITPCMK{suffix}"
    _seed_received_part_request(barcode, "ITestMake", "ITestModelXYZ")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/spare-parts", follow_redirects=True).text
        assert "<th>Tag Make</th><th>Tag Model</th>" in html
        assert html.index("<th>Tag Number</th>") < html.index("<th>Tag Make</th>") < html.index("<th>Lot Number</th>")
        assert "ITestMake" in html
        assert "ITestModelXYZ" in html
    finally:
        _cleanup_part_request(barcode)
