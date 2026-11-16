"""Final QC Fail form — Bucket Name (routers/cosmetic.py advance_stage):

 - 2026-08-27: dropped the earlier "must match reason+engineer" lock. Any
   number of tags with ANY mix of failure reasons and resolved engineers can
   now share the same Bucket Name — routing is a manual pick via the Assign
   Bucket modal afterward (see test_final_qc_assign_bucket_modal.py), not an
   auto-resolved reason/engineer pair, so there's nothing left to conflict.
 - Bucket.fail_reason / fail_engineer_* are still tracked (informational —
   feeds the Devices Failed table's "Engineer Name" column), just no longer
   enforced.
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


def _seed_device_at_final_qc(barcode):
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
        db.add(Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.final_qc))
        await db.commit()

asyncio.run(main())
""")


def _seed_workorder(barcode, stage_code, username):
    work_id = uuid.uuid4().hex[:12]
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.user import User
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        u = (await db.execute(select(User).where(User.username == "{username}"))).scalar_one()
        db.add(WorkOrder(work_id="{work_id}", device_id=dev.id, barcode="{barcode}",
                         stage="{stage_code}", assigned_role=u.role.value,
                         assigned_user_id=u.id, assigned_username=u.username,
                         assigned_name=u.full_name, status="completed", created_by="itest"))
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
from models.work_order import WorkOrder
from models.stock_transfer import StockTransfer

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for wo in (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all():
                await db.delete(wo)
            await db.flush()
            # Fail now routes on at once (2026-11-16), which can log a location transfer.
            for t in (await db.execute(select(StockTransfer).where(
                    StockTransfer.device_id == dev.id))).scalars().all():
                await db.delete(t)
            for m in (await db.execute(select(StageMovement).where(
                    StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def _device_state(barcode):
    out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.bucket import Bucket

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        b = (await db.execute(select(Bucket).where(Bucket.id == dev.bucket_id))).scalar_one_or_none()
        print("device.current_stage=" + dev.current_stage.value)
        print("bucket.fail_reason=" + str(b.fail_reason if b else None))
        print("bucket.fail_engineer_username=" + str(b.fail_engineer_username if b else None))

asyncio.run(main())
""")
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def _submit_fail(app_client, barcode, failure_reason, bucket_name):
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    return app_client.post("/cosmetic/advance", data={
        "csrf_token": csrf, "barcode": barcode, "final_qc_status": "fail",
        "failure_reason": failure_reason, "bucket_name": bucket_name,
    }, follow_redirects=False)


def test_multiple_tags_same_bucket_different_reason_and_engineer_all_succeed(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_1 = f"ITNOLOCK1{suffix}"
    barcode_2 = f"ITNOLOCK2{suffix}"
    bucket_name = f"ITestNoLock{suffix}"
    eng_a, _ = make_user("l1_engineer")
    eng_b, _ = make_user("qc_inspector")

    _seed_device_at_final_qc(barcode_1)
    _seed_workorder(barcode_1, "l1", eng_a)
    _seed_device_at_final_qc(barcode_2)
    _seed_workorder(barcode_2, "qc", eng_b)

    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        r1 = _submit_fail(app_client, barcode_1, "Hardware", bucket_name)
        assert r1.status_code == 302, r1.text[:400]

        # Different reason AND different resolved engineer, same bucket name
        # — must succeed now, no lock.
        r2 = _submit_fail(app_client, barcode_2, "Software", bucket_name)
        assert r2.status_code == 302, r2.text[:400]

        state2 = _device_state(barcode_2)
        # Fail now routes straight on by Failure Reason (2026-11-16): Software -> Stress Test.
        assert state2["device.current_stage"] == "qc_check"
        # Latest fail's resolution wins for the informational display.
        assert state2["bucket.fail_reason"] == "Software"
        assert state2["bucket.fail_engineer_username"] == eng_b
    finally:
        _cleanup_device(barcode_1)
        _cleanup_device(barcode_2)


def _park_in_fail_hold(barcode):
    """Put a tag back in Final QC — Fail Hold, like one failed before the
    Fail decision started routing straight on (2026-11-16); such tags still
    show in Devices Failed."""
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import update
from database import AsyncSessionLocal
from models.device import Device, DeviceStage

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(update(Device).where(Device.barcode == "{barcode}")
                         .values(current_stage=DeviceStage.final_qc_fail_hold))
        await db.commit()

asyncio.run(main())
""")


def test_bucket_engineer_name_still_shown_as_fyi(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITNOLOCKFYI{suffix}"
    bucket_name = f"ITestFyi{suffix}"
    eng_username, _ = make_user("l1_engineer")

    _seed_device_at_final_qc(barcode)
    _seed_workorder(barcode, "l1", eng_username)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        r = _submit_fail(app_client, barcode, "Hardware", bucket_name)
        assert r.status_code == 302, r.text[:400]
        _park_in_fail_hold(barcode)

        html = app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        row = html.split(bucket_name, 1)[1].split("</tr>", 1)[0]
        engineer_name = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.bucket import Bucket

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        b = (await db.execute(select(Bucket).where(Bucket.id == dev.bucket_id))).scalar_one()
        print(b.fail_engineer_name)

asyncio.run(main())
""")
        assert engineer_name in row
    finally:
        _cleanup_device(barcode)
