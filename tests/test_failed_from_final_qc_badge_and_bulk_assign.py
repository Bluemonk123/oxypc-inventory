""""Failed from Final QC" badge + filter (2026-09-02) across L1/L2 Repair,
Stress Test, Cosmetic Received, and Production Manager's Tag Number
Allocation tab — shown whenever Device.final_qc_status == "fail" (already
set by the Final QC Fail decision and left untouched until the tag goes
through Final QC again), no new column needed. Each page also gets a
"Failed from Final QC" checkbox filter in the table wrapper before the
search box (alongside L1/L2 Repair's pre-existing PNA checkbox, both moved
into the table wrapper together).

Production Manager's Tag Number Allocation tab additionally gets a Bulk
Assign (checkboxes + the existing Assign Device modal, Tag Number/Device
swapped for a Count of Selected Tags display) that assigns every selected
tag to one engineer and moves them all to L1/L2 Repair in one call
(POST /devices/bulk-assign-l1l2, routers/buckets.py).
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


def _seed_device(barcode, stage, final_qc_status=None):
    fqc_line = f'dev.final_qc_status = "{final_qc_status}"' if final_qc_status else "pass"
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
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{stage})
        {fqc_line}
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
from models.work_order import WorkOrder
from models.stock_transfer import StockTransfer

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for wo in (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all():
                await db.delete(wo)
            for m in (await db.execute(select(StageMovement).where(
                    StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            for t in (await db.execute(select(StockTransfer).where(
                    StockTransfer.device_id == dev.id))).scalars().all():
                await db.delete(t)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def _device_stage(barcode):
    return _run(f"""
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


def test_l1_repair_shows_badge_and_filter_checkboxes_in_wrapper(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_failed = f"ITL1FQC{suffix}"
    barcode_normal = f"ITL1NORM{suffix}"
    _seed_device(barcode_failed, "l1", final_qc_status="fail")
    _seed_device(barcode_normal, "l1")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/repair/l1", follow_redirects=True).text

        assert 'data-fqc-fail="1"' in html
        # Filters became dropdowns in the filter bar (PNA / Failed from Final QC).
        assert 'id="l1PnaFilter"' in html
        assert 'id="l1FailedFilter"' in html
        # The badge sits in the tag cell, after the spec-edit / parts icons.
        row = html.split(f'href="/devices/{barcode_failed}"', 1)[1].split("</td>", 1)[0]
        assert "Failed from Final QC" in row
        row2 = html.split(f'href="/devices/{barcode_normal}"', 1)[1].split("</td>", 1)[0]
        assert "Failed from Final QC" not in row2
    finally:
        _cleanup_device(barcode_failed)
        _cleanup_device(barcode_normal)


def test_stress_test_shows_badge_and_filter_checkbox(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITQCFQC{suffix}"
    _seed_device(barcode, "qc_check", final_qc_status="fail")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/qc", follow_redirects=True).text

        assert 'data-failed-fqc="1"' in html
        assert 'id="qcFailedFqcFilter"' in html
        row = html.split(f'href="/devices/{barcode}"', 1)[1].split("</td>", 1)[0]
        assert "Failed from Final QC" in row
    finally:
        _cleanup_device(barcode)


def test_cosmetic_received_shows_badge_and_filter_checkbox(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITRECVFQC{suffix}"
    _seed_device(barcode, "cosmetic_received", final_qc_status="fail")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/cosmetic/cosmetic_received", follow_redirects=True).text

        assert 'data-failed-fqc="1"' in html
        assert 'id="cosmeticRecvFailedFqcFilter"' in html
        # Tag shown as <code>, not a link, on this page.
        row = html.split(f'<code>{barcode}</code>', 1)[1].split("</td>", 1)[0]
        assert "Failed from Final QC" in row
    finally:
        _cleanup_device(barcode)


def test_trc_production_shows_badge_filter_and_bulk_assign_controls(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITTRCFQC{suffix}"
    _seed_device(barcode, "trc_production", final_qc_status="fail")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/trc-production", follow_redirects=True).text

        assert 'data-failed-fqc="1"' in html
        assert 'id="trcFailedFqcFilter"' in html
        assert 'id="trcSelectAll"' in html
        assert 'class="trcRowCheck"' in html
        assert 'id="trcBulkAssignBtn"' in html
        assert 'id="asgDevBulkFields"' in html
        assert 'id="asgDevBulkCount"' in html
        row = html.split(f'href="/devices/{barcode}"', 1)[1][:300]
        assert "Failed from Final QC" in row
    finally:
        _cleanup_device(barcode)


def test_bulk_assign_l1l2_moves_every_selected_tag_and_assigns_engineer(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_1 = f"ITTRCBULK1{suffix}"
    barcode_2 = f"ITTRCBULK2{suffix}"
    _seed_device(barcode_1, "trc_production", final_qc_status="fail")
    _seed_device(barcode_2, "trc_production", final_qc_status="fail")
    try:
        admin_username, admin_password = make_user("admin")
        eng_username, _ = make_user("l1_engineer")
        _login(app_client, admin_username, admin_password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        eng_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.user import User

async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "{eng_username}"))).scalar_one()
        print(u.id)

asyncio.run(main())
""")
        r = app_client.post("/devices/bulk-assign-l1l2", data={
            "csrf_token": csrf, "barcodes": f"{barcode_1},{barcode_2}",
            "department": "L1 Engineer", "assigned_user_id": eng_id,
        })
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert body["ok"] is True
        assert body["assigned"] == 2

        assert _device_stage(barcode_1) == "l1"
        assert _device_stage(barcode_2) == "l1"
    finally:
        _cleanup_device(barcode_1)
        _cleanup_device(barcode_2)
