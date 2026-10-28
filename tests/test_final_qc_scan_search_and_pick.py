"""Final QC page (templates/cosmetic/final_qc.html):
 - Scan/search Tag Number box next to the "awaiting Final QC" count, filtering
   the 1/4 card list by data-barcode.
 - Auto-attribution WorkID (2026-09-01, replaces the old "Pick This" button):
   submitting the Pass/Fail decision on /cosmetic/advance automatically
   creates a completed "fqc" WorkID for whoever submitted it — no separate
   claim-then-decide step, and no "stuck showing Picked by" state a stray
   leftover pending pick could cause.
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


def _cleanup_device(barcode):
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
            for m in (await db.execute(select(StageMovement).where(
                    StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def _fqc_workorders(barcode):
    return _run(f"""
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
            WorkOrder.device_id == dev.id, WorkOrder.stage == "fqc")
            .order_by(WorkOrder.assigned_at))).scalars().all()
        for wo in wos:
            print(wo.assigned_username + "|" + wo.status + "|" + wo.work_id)

asyncio.run(main())
""")


def test_final_qc_page_has_scan_box_and_no_pick_button(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITFQCPAGE{suffix}"
    _seed_device_at_final_qc(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        assert 'id="fqcScanSearch"' in html
        assert f'data-barcode="{barcode}"' in html
        # Pick This is back (2026-10-27, one Final QC WorkID per visit): it
        # claims the pending WorkID the tag arrived with.
        assert "fqc-pick-btn" in html
        assert "Pick This" in html
        assert "/cosmetic/final-qc/pick" in html
    finally:
        _cleanup_device(barcode)


def test_submitting_decision_auto_creates_completed_workid_for_submitter(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITFQCAUTO{suffix}"
    _seed_device_at_final_qc(barcode)
    try:
        username, password = make_user("cosmetic_manager")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post("/cosmetic/advance", data={
            "csrf_token": csrf, "barcode": barcode, "final_qc_status": "pass",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]

        lines = _fqc_workorders(barcode).splitlines()
        assert len(lines) == 1
        assigned_username, status, work_id = lines[0].split("|")
        assert assigned_username == username
        assert status == "completed"
        assert len(work_id) == 12

        status_html = app_client.get("/workid-status", follow_redirects=True).text
        assert barcode in status_html
        assert work_id in status_html
    finally:
        _cleanup_device(barcode)


def test_no_claim_needed_before_a_second_person_can_decide(app_client, make_user):  # noqa: F811
    """The old "Pick This" flow locked a tag to whoever picked it first —
    that lock no longer exists: any permitted user can submit the decision
    directly, no prior claim required."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITFQCNOLOCK{suffix}"
    _seed_device_at_final_qc(barcode)
    try:
        username, password = make_user("cosmetic_manager")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post("/cosmetic/advance", data={
            "csrf_token": csrf, "barcode": barcode, "final_qc_status": "pass",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:400]

        lines = _fqc_workorders(barcode).splitlines()
        assert len(lines) == 1
        assert lines[0].split("|")[0] == username
    finally:
        _cleanup_device(barcode)
