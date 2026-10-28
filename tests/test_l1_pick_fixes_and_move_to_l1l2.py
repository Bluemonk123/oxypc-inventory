"""2026-09-18 follow-up batch:

L1/L2 "Pick This" bug fixes (routers/repair.py::l1_pick):
 - Now writes a same-stage (l1 -> l1) StageMovement on pick, so the action is
   captured in Tag Asset History (/devices/api/asset-history) instead of
   leaving no trace at all.
 - Because /workid-status's Assigned Engineer column is sourced from the
   device's latest StageMovement (not WorkOrder.assigned_username), the new
   movement also fixes that column never updating to the picking user.

/workid-status default sort (routers/workid_status.py): now sorted by
Assigned Date descending (was accidentally sorted by Completed Date first,
which could put rows out of Assigned Date order).

Inventory Manager (/stock) new actions (routers/stock.py):
 - "Move to L1/L2" button per row in Inventory Stock's Action column.
 - "Bulk Move to L1/L2 <count>" button in the card header.
 - Both move Stock In devices to DeviceStage.l1, unassigned (no WorkOrder),
   so the tag lands in /repair/l1's unclaimed pool and shows "Pick This".
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


def _seed_device(barcode, stage, moved_by="itest_seed_mover"):
    """A device with ONE prior StageMovement into `stage`, moved by someone
    other than the picker — so a test that later picks/moves it can assert
    the Engineer column actually changed to the acting user, not just that
    it already happened to match."""
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{stage})
        db.add(dev)
        await db.flush()
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.grn,
                              to_stage=DeviceStage.{stage}, moved_by="{moved_by}"))
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
from models.device import Device
from models.work_order import WorkOrder
from models.device import StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for wo in (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all():
                await db.delete(wo)
            for mv in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(mv)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_pick_this_writes_stage_movement_visible_in_asset_history(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITPICKMV{suffix}"
    device_id = _seed_device(barcode, "l1")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/repair/l1/pick", data={"csrf_token": csrf, "device_id": device_id})
        assert r.status_code in (200, 302)

        hist = app_client.get(f"/devices/api/asset-history?barcode={barcode}").json()
        assert hist["found"] is True
        assert any("Picked by" in (mv.get("notes") or "") for mv in hist["movements"])
    finally:
        _cleanup(barcode)


def test_pick_this_updates_assigned_engineer_on_workid_status(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITPICKENG{suffix}"
    device_id = _seed_device(barcode, "l1", moved_by="itest_seed_mover")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        app_client.post("/repair/l1/pick", data={"csrf_token": csrf, "device_id": device_id})

        work_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        wo = (await db.execute(select(WorkOrder).where(WorkOrder.device_id == "{device_id}"))).scalar_one()
        print(wo.work_id)

asyncio.run(main())
""")

        full_name = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.user import User

async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "{username}"))).scalar_one()
        print(u.full_name or u.username)

asyncio.run(main())
""")

        html = app_client.get(f"/workid-status?workid={work_id}", follow_redirects=True).text
        # work_id also appears earlier in the page, as the filter form's own
        # search-box value= — rfind so `row` is sliced from the actual table
        # row (the LAST occurrence), not that filter input.
        idx = html.rfind(work_id)
        row = html[idx:idx + 2000]
        assert "itest_seed_mover" not in row
        # Engineer column renders the resolved display name, not the raw
        # username (see workid_status.py's display_name_by_username lookup).
        assert full_name in row
    finally:
        _cleanup(barcode)


def test_workid_status_default_sort_is_assigned_date_descending(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    older_bc = f"ITSORTOLD{suffix}"
    newer_bc = f"ITSORTNEW{suffix}"
    older_id = _seed_device(older_bc, "l1")
    newer_id = _seed_device(newer_bc, "l1")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        # Older WorkOrder gets an assigned_at in the past; newer stays "now".
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from datetime import timedelta
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.work_order import WorkOrder
from routers.transfers import _gen_work_id
from utils.timezone import app_now

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.id == "{older_id}"))).scalar_one()
        wid = await _gen_work_id(db)
        db.add(WorkOrder(work_id=wid, device_id=dev.id, barcode=dev.barcode, stage="l1",
                          assigned_username="{username}", assigned_name="{username}",
                          status="pending", created_by="{username}",
                          assigned_at=app_now() - timedelta(days=5)))
        await db.commit()
        print(wid)

asyncio.run(main())
""")
        app_client.post("/repair/l1/pick", data={"csrf_token": csrf, "device_id": newer_id})

        html = app_client.get("/workid-status", follow_redirects=True).text
        pos_new = html.find(newer_bc)
        pos_old = html.find(older_bc)
        assert pos_new != -1 and pos_old != -1
        assert pos_new < pos_old  # newer Assigned Date renders first
    finally:
        _cleanup(older_bc)
        _cleanup(newer_bc)


def test_stock_move_to_l1l2_single_row_shows_pick_this(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVL1{suffix}"
    _seed_device(barcode, "stock_in", moved_by="itest_seed_mover")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/stock/move-to-l1l2", data={"csrf_token": csrf, "barcode": barcode},
                             follow_redirects=False)
        assert r.status_code == 302

        stage = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        print(dev.current_stage.value)

asyncio.run(main())
""")
        assert stage == "l1"

        html = app_client.get("/repair/l1", follow_redirects=True).text
        row = html.split(barcode, 1)[1][:4000]
        assert "Pick This" in row
    finally:
        _cleanup(barcode)


def test_stock_move_to_l1l2_closes_out_stale_uncompleted_l1_work_order(app_client, make_user):  # noqa: F811
    """Real data has plenty of Stock In tags carrying an old "l1" WorkOrder
    that was never marked completed (the device cycled through L1/L2 before,
    then got sent back to Stock In some other way). /repair/l1's queue keys
    off "does this device have a non-completed l1 WorkOrder" — so without
    closing that stale one out, Move to L1/L2 would land the tag back in the
    queue still showing the OLD WorkID/engineer instead of Pick This."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVSTALE{suffix}"
    device_id = _seed_device(barcode, "stock_in", moved_by="itest_seed_mover")
    stale_work_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.work_order import WorkOrder
from routers.transfers import _gen_work_id

async def main():
    async with AsyncSessionLocal() as db:
        wid = await _gen_work_id(db)
        db.add(WorkOrder(work_id=wid, device_id="{device_id}", barcode="{barcode}", stage="l1",
                          assigned_username="itest_stale_engineer", assigned_name="Stale Engineer",
                          status="pending", created_by="itest_stale_engineer"))
        await db.commit()
        print(wid)

asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/stock/move-to-l1l2", data={"csrf_token": csrf, "barcode": barcode},
                             follow_redirects=False)
        assert r.status_code == 302

        html = app_client.get("/repair/l1", follow_redirects=True).text
        row = html.split(barcode, 1)[1][:4000]
        assert "Pick This" in row
        assert stale_work_id not in row
        assert "itest_stale_engineer" not in row and "Stale Engineer" not in row

        status = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        wo = (await db.execute(select(WorkOrder).where(WorkOrder.work_id == "{stale_work_id}"))).scalar_one()
        print(wo.status)

asyncio.run(main())
""")
        assert status == "completed"
    finally:
        _cleanup(barcode)


def test_stock_bulk_move_to_l1l2_moves_multiple_devices(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    bc1 = f"ITBULKMV1{suffix}"
    bc2 = f"ITBULKMV2{suffix}"
    _seed_device(bc1, "stock_in")
    _seed_device(bc2, "stock_in")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/stock/bulk-move-to-l1l2",
                             data={"csrf_token": csrf, "barcodes": f"{bc1},{bc2}"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["moved"] == 2

        stages = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{bc1}", "{bc2}"):
            dev = (await db.execute(select(Device).where(Device.barcode == bc))).scalar_one()
            print(dev.current_stage.value)

asyncio.run(main())
""")
        assert stages.splitlines() == ["l1", "l1"]
    finally:
        _cleanup(bc1)
        _cleanup(bc2)
