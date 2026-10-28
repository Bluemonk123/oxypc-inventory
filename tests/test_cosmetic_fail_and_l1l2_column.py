"""Cleaning and Water Sanding pages (templates/cosmetic/stage.html):
 - "L1/L2 Engineer" column after Tag Number, same resolution as the Stress
   Test page (most recent WorkOrder at stage="l1").
 - Fail itself was later removed from Cleaning/Water Sanding's own page UI
   (see tests/test_cosmetic_move_assignment_and_group_visibility.py), but the
   POST /cosmetic/{barcode}/fail endpoint itself is unchanged and still
   accepts a device sitting at water_sanding (only Cosmetic Received/
   Completed keep a Fail button in the UI now) — the tests below that post
   to the endpoint directly are still valid.
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


_SEED_SRC = """
import asyncio, sys
sys.path.insert(0, r"{root}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.lot import Lot
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{stage})
        db.add(dev)
        await db.flush()
        db.add(WorkOrder(work_id="ITW{suffix}", device_id=dev.id, barcode=dev.barcode,
                         stage="l1", assigned_name="Prior Engineer", status="completed"))
        await db.commit()

asyncio.run(main())
"""

_CLEANUP_SRC = """
import asyncio, sys
sys.path.insert(0, r"{root}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            wos = (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all()
            for wo in wos:
                await db.delete(wo)
            movements = (await db.execute(select(StageMovement).where(
                StageMovement.device_id == dev.id))).scalars().all()
            for m in movements:
                await db.delete(m)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
"""


def test_l1l2_engineer_column_and_fail_modal_on_cleaning_page(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITESTCLEAN{suffix}"

    _run(_SEED_SRC.format(root=ROOT, barcode=barcode, stage="cleaning", suffix=suffix))
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/cosmetic/cleaning", follow_redirects=True).text

        assert "<th>L1/L2 Engineer</th>" in html
        assert html.index("<th>Tag Number</th>") < html.index("<th>L1/L2 Engineer</th>") < html.index("<th>Brand</th>")

        row = html.split(barcode, 1)[1].split("</tr>", 1)[0]
        assert "Prior Engineer" in row

        # Fail (and the old "Done & Move to Final QC" skip button) were later
        # removed from this page — see
        # tests/test_cosmetic_move_assignment_and_group_visibility.py
        # test_cleaning_page_no_longer_has_move_to_final_qc_or_fail.
        assert "cosmeticFailModal" not in html
        assert "openFailModal(" not in row
    finally:
        _run(_CLEANUP_SRC.format(root=ROOT, barcode=barcode))


def test_fail_endpoint_assigns_engineer_and_moves_to_l1(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITESTWS{suffix}"

    _run(_SEED_SRC.format(root=ROOT, barcode=barcode, stage="water_sanding", suffix=suffix))
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        make_user("l1_engineer")   # an active L1 engineer exists even on an empty DB

        eng_row = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.user import User, UserRole

async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(
            User.role == UserRole.l1_engineer, User.status == True))).scalars().first()
        print(u.id if u else "")

asyncio.run(main())
""")
        assert eng_row, "need at least one active l1_engineer in the DB for this test"

        r = app_client.post(f"/cosmetic/{barcode}/fail", data={
            "csrf_token": csrf, "engineer_user_id": eng_row, "notes": "cosmetic defect found",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]

        check = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        wos = (await db.execute(select(WorkOrder).where(
            WorkOrder.device_id == dev.id, WorkOrder.stage == "l1"))).scalars().all()
        print(dev.current_stage.value)
        print(dev.repair_notes)
        print(len(wos))

asyncio.run(main())
""")
        lines = check.splitlines()
        assert lines[0] == "l1"
        assert "Water Sanding Failed" in lines[1]
        assert "cosmetic defect found" in lines[1]
        assert int(lines[2]) == 2  # the seeded prior WorkOrder + the new one from this Fail
    finally:
        _run(_CLEANUP_SRC.format(root=ROOT, barcode=barcode))


def test_fail_rejects_device_not_at_cleaning_or_water_sanding(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITESTPAINT{suffix}"

    _run(_SEED_SRC.format(root=ROOT, barcode=barcode, stage="painting", suffix=suffix))
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post(f"/cosmetic/{barcode}/fail", data={
            "csrf_token": csrf, "engineer_user_id": str(uuid.uuid4()), "notes": "",
        }, follow_redirects=False)
        assert r.status_code == 400, r.text[:300]
    finally:
        _run(_CLEANUP_SRC.format(root=ROOT, barcode=barcode))
