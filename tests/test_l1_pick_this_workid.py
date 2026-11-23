"""L1/L2 Repair page (/repair/l1), 2026-09-18:

A tag sitting in the merged L1/L2 queue with no WorkOrder yet (the
unclaimed pool) now shows a "Pick This" button in the WorkID column
instead of a bare "—". Clicking it (POST /repair/l1/pick) creates a
WorkOrder for the CURRENT user — no engineer-picker modal, same WorkOrder
shape as every other plain L1/L2 pick (stage "l1", 12-digit numeric WorkID)
— after which the row shows WorkID + Assigned Engineer like any other
picked tag, and the new WorkID shows up on /workid-status too.
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


def _seed_l1_device(barcode):
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
                     current_stage=DeviceStage.l1)
        db.add(dev)
        await db.commit()
        print(dev.id)

asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for wo in (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all():
                await db.delete(wo)
            # Pick This now writes a StageMovement (2026-09-18) — purge those
            # too, or deleting the device below nulls their NOT NULL
            # device_id column instead of a clean cascade.
            for mv in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(mv)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_pick_this_button_shown_for_unclaimed_l1_tag(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITPICKBTN{suffix}"
    _seed_l1_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/repair/l1", follow_redirects=True).text
        assert barcode in html
        # The row's WorkID cell contains a Pick This form for this specific
        # device, not just the button existing somewhere else on the page.
        row = html.split(barcode, 1)[1][:1500]
        assert "Pick This" in row
        assert '/repair/l1/pick' in row
    finally:
        _cleanup(barcode)


def test_pick_this_creates_workorder_and_replaces_button(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITPICKGO{suffix}"
    _seed_l1_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        device_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        print(dev.id)

asyncio.run(main())
""")
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/repair/l1/pick", data={
            "csrf_token": csrf, "device_id": device_id,
        }, follow_redirects=False)
        assert r.status_code == 302

        work_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        wo = (await db.execute(select(WorkOrder).where(WorkOrder.device_id == "{device_id}"))).scalar_one()
        assert wo.stage == "l1"
        assert wo.status == "pending"
        assert wo.assigned_username == "{username}"
        print(wo.work_id)

asyncio.run(main())
""")
        assert work_id and len(work_id) == 12 and work_id.isdigit()

        html = app_client.get("/repair/l1", follow_redirects=True).text
        row = html.split(barcode, 1)[1][:1500]
        assert "Pick This" not in row
        assert work_id in row

        # Same WorkID visible on /workid-status.
        wid_html = app_client.get(f"/workid-status?workid={work_id}&exclude_admin=0", follow_redirects=True).text
        assert work_id in wid_html
        assert barcode in wid_html
    finally:
        _cleanup(barcode)


def test_pick_this_is_idempotent_against_double_submit(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITPICKDBL{suffix}"
    _seed_l1_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        device_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        print(dev.id)

asyncio.run(main())
""")
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        app_client.post("/repair/l1/pick", data={"csrf_token": csrf, "device_id": device_id})
        app_client.post("/repair/l1/pick", data={"csrf_token": csrf, "device_id": device_id})

        count = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, func
from database import AsyncSessionLocal
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        c = (await db.execute(select(func.count(WorkOrder.id)).where(WorkOrder.device_id == "{device_id}"))).scalar()
        print(c)

asyncio.run(main())
""")
        assert count == "1"
    finally:
        _cleanup(barcode)
