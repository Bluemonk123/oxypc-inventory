"""2026-09-21 batch:
 - Dashboard Stage Pipeline gains a "Split Location" toggle, alongside the
   existing "Split Entity" one — same single-line-per-row treatment, but
   grouped by each device's CURRENT location (latest DeviceLocationLog row,
   falling back to Device.location_id only when no log exists — the same
   resolution the Location ID filter itself already uses).
 - Root-caused "Sale record not found" when returning an already-sold tag:
   the device was really sitting at current_stage=sold with ZERO Sale rows,
   because DROPDOWN_STAGES (the option list behind every generic "move a
   device to any stage" tool — Manual Stage Movement, the IQC/Bulk Customise
   modal's bulk Move-to-Stage) used to include "Sold", and none of those
   tools create the Sale row that stage actually requires. Fixed the root
   cause by excluding DeviceStage.sold from DROPDOWN_STAGES. Per explicit
   instruction, process_return no longer blocks this case either: if a
   device is at current_stage=sold with no Sale row, it auto-creates a
   placeholder Sale (blank customer details, sale_price 0) so the return —
   or a Replace Now on it — can still go through instead of dead-ending.
 - Parts Consumption's export button is relabelled "Tags Summary" (same
   per-tag-per-part export as before) and gains a sibling "Parts Summary"
   button that aggregates by Part Name across every tag it was used on.
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


# ────────────────────── AREA: Dashboard Split Location ───────────────────────

def _seed_device_with_location(barcode, unit_id, username):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.location import StorageLocation, DeviceLocationLog, LocationAction, ZoneType, UnitType
from models.user import User

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        user = (await db.execute(select(User).where(User.username == "{username}"))).scalar_one()
        loc = StorageLocation(zone=ZoneType.workshop, unit_type=UnitType.shelf, unit_id="{unit_id}")
        db.add(loc)
        await db.flush()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.ready_to_sale)
        db.add(dev)
        await db.flush()
        db.add(DeviceLocationLog(device_id=dev.id, location_id=loc.id, action=LocationAction.assigned,
                                  actor_id=user.id, actor_name=user.username))
        await db.commit()

asyncio.run(main())
""")


def _cleanup_device_and_location(barcode, unit_id):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.location import StorageLocation, DeviceLocationLog

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for log in (await db.execute(select(DeviceLocationLog).where(
                    DeviceLocationLog.device_id == dev.id))).scalars().all():
                await db.delete(log)
            await db.delete(dev)
        loc = (await db.execute(select(StorageLocation).where(StorageLocation.unit_id == "{unit_id}"))).scalar_one_or_none()
        if loc:
            await db.delete(loc)
        await db.commit()

asyncio.run(main())
""")


def test_dashboard_has_split_location_button(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/dashboard", follow_redirects=True).text
    assert 'id="splitLocationToggle"' in html
    assert "Split Location" in html
    assert "split-location-detail" in html


def test_dashboard_split_location_breakdown_includes_seeded_location(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITSPLITLOC{suffix}"
    unit_id = f"ITESTSHELF{suffix}"
    username, password = make_user("admin")
    _seed_device_with_location(barcode, unit_id, username)
    try:
        _login(app_client, username, password)
        html = app_client.get("/dashboard", follow_redirects=True).text
        assert unit_id in html
    finally:
        _cleanup_device_and_location(barcode, unit_id)


# ──────────────────── AREA: sold-without-sale root cause fix ─────────────────

def test_dropdown_stages_excludes_sold(app_client, make_user):  # noqa: F811
    """Manual Stage Movement's dropdown must not offer 'Sold' — that stage
    requires a real Sale row, which this generic tool never creates."""
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/repair/move/form", follow_redirects=True).text
    assert 'value="sold"' not in html
    # Sanity: the dropdown still offers ordinary stages.
    assert 'value="iqc"' in html


def _seed_sold_device_no_sale(barcode):
    return _run(f"""
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
                     current_stage=DeviceStage.sold)
        db.add(dev)
        await db.commit()

asyncio.run(main())
""")


def _cleanup_device(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement
from models.sales import Sale, Return

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            # Sale.device_id and StageMovement.device_id are NOT NULL with no
            # cascade — delete referencing rows before the device itself, or
            # the ORM tries to null the FK first and trips the constraint.
            for ret in (await db.execute(select(Return).where(Return.device_id == dev.id))).scalars().all():
                await db.delete(ret)
            for sale in (await db.execute(select(Sale).where(Sale.device_id == dev.id))).scalars().all():
                await db.delete(sale)
            for m in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            await db.flush()
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_process_return_sold_without_sale_succeeds_with_placeholder_sale(app_client, make_user):  # noqa: F811
    """Per explicit instruction: don't block, still process the return —
    attaching it to an auto-created placeholder Sale with blank customer
    details, since there's no real sale to pull them from."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITORPHSOLD{suffix}"
    _seed_sold_device_no_sale(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/returns/new", data={
            "csrf_token": csrf, "barcode": barcode,
            "reason": "Physical damage", "condition_on_return": "Minor damage",
            "replace_now": "no",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:800]
        assert "success" in r.headers.get("location", "")

        state = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Sale, Return

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        sale = (await db.execute(select(Sale).where(Sale.device_id == dev.id))).scalars().first()
        ret = (await db.execute(select(Return).where(Return.device_id == dev.id))).scalars().first()
        print(sale is not None)
        print(sale.customer_name is None)
        print(ret is not None)
        print(ret.sale_id == sale.id)

asyncio.run(main())
""").splitlines()
        assert state == ["True", "True", "True", "True"], state
    finally:
        _cleanup_return_placeholder(barcode)


def test_process_return_replace_now_on_sold_without_sale_uses_blank_customer_details(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITORPHSOLD2{suffix}"
    replace_barcode = f"ITORPHREPL{suffix}"
    _seed_sold_device_no_sale(barcode)
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
        dev = Device(barcode="{replace_barcode}", lot_id=lot.id, brand="X", model="Y",
                     current_stage=DeviceStage.ready_to_sale)
        db.add(dev)
        await db.commit()

asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/returns/new", data={
            "csrf_token": csrf, "barcode": barcode,
            "reason": "Physical damage", "condition_on_return": "Minor damage",
            "replace_now": "yes", "replace_tag": replace_barcode,
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:800]
        assert "success" in r.headers.get("location", "")

        state = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Sale

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{replace_barcode}"))).scalar_one()
        sale = (await db.execute(select(Sale).where(Sale.device_id == dev.id))).scalar_one()
        print(dev.current_stage.value)
        print(sale.customer_name is None)
        print(sale.customer_phone is None)

asyncio.run(main())
""").splitlines()
        assert state == ["sold", "True", "True"], state
    finally:
        _cleanup_return_placeholder(barcode)
        _cleanup_device(replace_barcode)


def _cleanup_return_placeholder(barcode):
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
            for ret in (await db.execute(select(Return).where(Return.device_id == dev.id))).scalars().all():
                await db.delete(ret)
            for sale in (await db.execute(select(Sale).where(Sale.device_id == dev.id))).scalars().all():
                await db.delete(sale)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


# ─────────────────── AREA: Parts Consumption export split ────────────────────

def test_parts_consumption_export_buttons_renamed_and_added(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/spare-parts", follow_redirects=True).text
    # 2026-10-29: Tags Summary + Parts Summary merged into one
    # "Consumption Summary" export (two sheets), beside "Repair Summary".
    assert "Consumption Summary" in html and "Repair Summary" in html
    assert 'id="pcExportPartsSummaryBtn"' not in html
    assert "/spare-parts/consumption-summary" in html
    # Old bare "Export" label for this specific button is gone.
    assert 'spExport(\'partsConsumptionTable\',false)">\n      <i class="bi bi-file-earmark-arrow-down me-1"></i>Export' not in html
