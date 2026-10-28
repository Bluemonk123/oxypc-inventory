"""2026-09-19: L1/L2 <-> L3/L4 hand-off now moves Device.current_stage, not
just the l1l2_status/l34_status display columns.

Previously request_l3l4 / l3l4_complete / l3l4_scrap left current_stage
untouched (see the docstring in test_workid_status_l3l4_stage_and_multiselect.py
for the old, deliberate rationale). That meant a tag "Requested to L3/L4"
never actually left /repair/l1's query (Device.current_stage ==
DeviceStage.l1), so the row stayed visible showing a stale Status badge and
offered a "Start Repair" button it had no business offering while the tag
was genuinely out at the L3/L4 bench.

Fixed:
 - request_l3l4 moves the device to DeviceStage.l3 -> row drops out of
   /repair/l1 (query-level hide, no template change needed).
 - l3l4_complete moves it back to DeviceStage.l1 -> row reappears with
   Status "Returned from L3/L4" and L3/L4 Status "Completed".
 - l3l4_scrap also moves it back to DeviceStage.l1, so the existing
   "Back to Inventory" button (repair/l1.html, branches on l34_status) stays
   reachable -- that button only ever lived on the L1/L2 page.
 - Each transition writes a StageMovement (Tag Asset History), matching the
   convention used by every other stage-changing action in this router.
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


def _seed_device_in_repair(barcode):
    """A device at L1, Repair Started -- the state Request to L3/L4 is
    actually raised from in the UI."""
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
                     current_stage=DeviceStage.l1, l1l2_status="Repair Started")
        db.add(dev)
        await db.commit()
        print(dev.id)

asyncio.run(main())
""")


def _device_stage(device_id):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.id == "{device_id}"))).scalar_one()
        print(dev.current_stage.value)

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
            for mv in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(mv)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_request_l3l4_moves_device_to_l3_and_hides_row_from_l1_page(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITL3MV{suffix}"
    device_id = _seed_device_in_repair(barcode)
    try:
        admin_user, admin_pass = make_user("admin")
        l3_username, _ = make_user("l3_engineer")
        _login(app_client, admin_user, admin_pass)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        # Visible before the request
        assert barcode in app_client.get("/repair/l1", follow_redirects=True).text

        r = app_client.post("/repair/request-l3l4", data={
            "csrf_token": csrf, "device_id": device_id,
            "assigned_l3l4_username": l3_username,
        }, follow_redirects=False)
        assert r.status_code == 302, r.text

        assert _device_stage(device_id) == "l3"
        assert barcode not in app_client.get("/repair/l1", follow_redirects=True).text
    finally:
        _cleanup(barcode)


def test_l3l4_complete_moves_device_back_to_l1_and_row_reappears(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITL3CMP{suffix}"
    device_id = _seed_device_in_repair(barcode)
    try:
        admin_user, admin_pass = make_user("admin")
        l3_username, _ = make_user("l3_engineer")
        _login(app_client, admin_user, admin_pass)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        app_client.post("/repair/request-l3l4", data={
            "csrf_token": csrf, "device_id": device_id,
            "assigned_l3l4_username": l3_username,
        })
        assert _device_stage(device_id) == "l3"

        r = app_client.post("/repair/l3l4-complete", data={
            "csrf_token": csrf, "device_id": device_id,
        }, follow_redirects=False)
        assert r.status_code == 302, r.text

        assert _device_stage(device_id) == "l1"
        html = app_client.get("/repair/l1", follow_redirects=True).text
        assert barcode in html
        assert "Returned from L3/L4" in html
    finally:
        _cleanup(barcode)


def test_l3l4_scrap_moves_device_back_to_l1_so_back_to_inventory_shows(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITL3SCR{suffix}"
    device_id = _seed_device_in_repair(barcode)
    try:
        admin_user, admin_pass = make_user("admin")
        l3_username, _ = make_user("l3_engineer")
        _login(app_client, admin_user, admin_pass)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        app_client.post("/repair/request-l3l4", data={
            "csrf_token": csrf, "device_id": device_id,
            "assigned_l3l4_username": l3_username,
        })
        assert _device_stage(device_id) == "l3"

        r = app_client.post("/repair/l3l4-scrap", data={
            "csrf_token": csrf, "device_id": device_id, "scrap_type": "Normal Scrap",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text

        assert _device_stage(device_id) == "l1"
        html = app_client.get("/repair/l1", follow_redirects=True).text
        row = html.split(f'data-device-id="{device_id}"')[1].split("</tr>")[0]
        assert "Back to Inventory" in row
    finally:
        _cleanup(barcode)


def test_request_l3l4_writes_stage_movement_visible_in_asset_history(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITL3HIST{suffix}"
    device_id = _seed_device_in_repair(barcode)
    try:
        admin_user, admin_pass = make_user("admin")
        l3_username, _ = make_user("l3_engineer")
        _login(app_client, admin_user, admin_pass)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        app_client.post("/repair/request-l3l4", data={
            "csrf_token": csrf, "device_id": device_id,
            "assigned_l3l4_username": l3_username,
        })

        hist = app_client.get(f"/devices/api/asset-history?barcode={barcode}").json()
        assert hist["found"] is True
        assert any("Requested to L3/L4" in (mv.get("notes") or "") for mv in hist["movements"])
    finally:
        _cleanup(barcode)


def test_manual_move_off_l3_closes_orphaned_l3l4_workorder(app_client, make_user):  # noqa: F811
    """routers/repair.py's generic Manual Stage Movement tool (/repair/move,
    admin-only) is the one other path that can take a device off L3 without
    going through l3l4_complete/l3l4_scrap -- the only two places that used
    to close an open "L3L4-" WorkOrder. Without this, the WorkOrder is
    orphaned: still open, still showing on /repair/l3l4 and counted in
    Production Manager's "Total Tags in L3/L4" tile's underlying query, even
    though the device has moved to an unrelated stage. Found via 17 real
    production tags in exactly this state (2026-09-19)."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITL3ORPH{suffix}"
    device_id = _seed_device_in_repair(barcode)
    try:
        admin_user, admin_pass = make_user("admin")
        l3_username, _ = make_user("l3_engineer")
        _login(app_client, admin_user, admin_pass)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        app_client.post("/repair/request-l3l4", data={
            "csrf_token": csrf, "device_id": device_id,
            "assigned_l3l4_username": l3_username,
        })
        assert _device_stage(device_id) == "l3"

        r = app_client.post("/repair/move", data={
            "csrf_token": csrf, "barcode": barcode, "to_stage": "qc_check",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text

        wo_status = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        wo = (await db.execute(select(WorkOrder).where(
            WorkOrder.device_id == "{device_id}", WorkOrder.work_id.like("L3L4-%")
        ))).scalars().first()
        print(wo.status if wo else "NONE")

asyncio.run(main())
""")
        # A manual move takes the tag off its L3/L4 engineer: closed as
        # "reassigned" since 2026-10-23 (not counted as their finished work).
        assert wo_status == "reassigned"
    finally:
        _cleanup(barcode)
