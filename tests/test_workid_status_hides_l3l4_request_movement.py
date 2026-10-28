"""2026-09-22: request_l3l4's own "Requested to L3/L4" StageMovement (added
after tests/test_workid_status_l3l4_stage_and_multiselect.py's docstring was
written, to move current_stage to L3 -- see test_l3l4_stage_transitions.py)
was never marked "used" by the L3L4- WorkOrder's handoff-stage display path,
so it leaked through /workid-status's backfill query as a phantom extra row
attributed to the L1/L2 requester -- duplicate to the L3L4- WorkOrder row
that already shows the same request under the assigned L3/L4 engineer.

Fixed: routers/workid_status.py's _is_l3l4_request_movement() excludes that
movement from both Asset-History matching and the backfill candidate pool.
The L3L4- WorkOrder row itself (L3/L4-assigned WorkID status) must still show.
"""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.workid_helpers import export_sheet, page_rows, rows_by_workid  # noqa: F401

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def _seed_device_in_repair(barcode):
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


def test_l3l4_request_movement_does_not_leak_as_phantom_backfill_row(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITL3WID{suffix}"
    device_id = _seed_device_in_repair(barcode)
    try:
        admin_user, admin_pass = make_user("admin")
        l3_username, _ = make_user("l3_engineer")
        _login(app_client, admin_user, admin_pass)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post("/repair/request-l3l4", data={
            "csrf_token": csrf, "device_id": device_id,
            "assigned_l3l4_username": l3_username,
        }, follow_redirects=False)
        assert r.status_code == 302, r.text

        # Bound by tag so the backfill path actually runs (unbounded loads
        # never backfill at all -- this test must exercise the code path
        # where the bug showed up). Deliberately no completed_from/to: the
        # WorkOrder is still "pending" (no completed_at yet), and the page's
        # Completed Date filter drops any row with no completed_at -- adding
        # date bounds here would hide the very row this test checks for.
        html = app_client.get(f"/workid-status?tag={barcode}", follow_redirects=True).text

        # The L3/L4-assigned WorkID must still show.
        rows = page_rows(html)
        assert any(r["w"].startswith("L3L4-") for r in rows)

        # No backfilled movement row for this device's synthetic
        # "Requested to L3/L4" movement.
        assert not any(r["k"].startswith("mv-") for r in rows)
    finally:
        _cleanup(barcode)
