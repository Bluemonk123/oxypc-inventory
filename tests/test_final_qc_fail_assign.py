"""Final QC "Devices Failed" tab (2026-08-29 redesign):

 - One row PER TAG (Tag Number column), not grouped by bucket — since the
   bucket-lock removal, a Bucket Name can hold tags with different Failure
   Reasons / resolved engineers, so a bucket-level row can't show a single
   correct Engineer Name or be moved as one unit any more.
 - A "Fail Note" column (renamed from "Final Notes" 2026-09-03, on both the
   form and this table) shows the raw text from the Final QC fail form's
   Fail Note field (Device.fqc_final_notes — field/column names unchanged,
   only the visible label).
 - The per-row button is "Move" (renamed from "Assign") — no modal. It posts
   the tag's own barcode to /cosmetic/final-qc/move-failed, which routes by
   the tag's OWN Failure Reason (Hardware -> L1/L2 Repair, Software ->
   Stress Test, Cosmetic -> Cosmetic Repair) and hands it to the tag's OWN
   resolved Engineer Name (same lookup that fills the table's Engineer Name
   column). Only Hardware has a "nobody to hand it to" fallback: a blank
   Engineer Name there parks the tag in Production Manager's Tag Number
   Allocation queue (DeviceStage.trc_production) instead of moving it to
   L1/L2 Repair with no owner. Software/Cosmetic always move to Stress
   Test/Cosmetic Received respectively, engineer resolved or not — those
   stages' own queues pick it up either way.
 - "Bulk Move" posts the same endpoint with every checked barcode
   comma-joined — each tag still routes independently by its own reason,
   even when two checked tags share a Bucket Name but different reasons.
 - A search box filters rows by Bucket Name or Tag Number, and a "No
   Engineer" checkbox filters to rows with a blank Engineer Name (both
   client-side).
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


def _device_state(barcode):
    out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        # Exclude "fqc" — that's the Final QC decision's own auto-attribution
        # WorkOrder (whoever submitted Pass/Fail), unrelated to who the tag
        # got handed to at its NEW stage after this Move.
        wo = (await db.execute(select(WorkOrder).where(
              WorkOrder.device_id == dev.id, WorkOrder.stage != "fqc")
              .order_by(WorkOrder.assigned_at.desc()))).scalars().first()
        print("stage=" + dev.current_stage.value)
        print("assigned_username=" + str(wo.assigned_username if wo else None))

asyncio.run(main())
""")
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def _submit_fail(app_client, barcode, failure_reason, bucket_name, notes=""):
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    r = app_client.post("/cosmetic/advance", data={
        "csrf_token": csrf, "barcode": barcode, "final_qc_status": "fail",
        "failure_reason": failure_reason, "bucket_name": bucket_name, "notes": notes,
    }, follow_redirects=False)
    assert r.status_code == 302, r.text[:400]


def _park_in_fail_hold(barcode):
    """Put a tag back in Final QC — Fail Hold, like one failed before the
    Fail decision started routing straight on (2026-11-16)."""
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


def _move_failed(app_client, barcodes):
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    return app_client.post("/cosmetic/final-qc/move-failed", data={
        "csrf_token": csrf, "barcodes": ",".join(barcodes),
    })


def test_devices_failed_table_lists_each_tag_as_its_own_row_with_move_button(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITFQCTBL{suffix}"
    bucket_name = f"ITestTblBkt{suffix}"
    eng_username, _ = make_user("l1_engineer")

    _seed_device_at_final_qc(barcode)
    _seed_workorder(barcode, "l1", eng_username)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Hardware", bucket_name, notes="Cracked hinge, keeps rebooting")
        # Routed on straight away (2026-11-16) — not listed in Devices Failed.
        assert _device_state(barcode)["stage"] == "l1"
        assert barcode not in app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        # A tag still waiting in Fail Hold (failed before the change) keeps its row + Move button.
        _park_in_fail_hold(barcode)

        html = app_client.get("/cosmetic/final_qc", follow_redirects=True).text
        assert 'id="fqcFailedSearch"' in html
        assert 'id="fqcNoEngineerFilter"' in html
        assert 'id="fqcBulkMoveBtn"' in html
        assert 'class="fqc-failed-check"' in html
        assert 'class="btn btn-sm btn-primary fqc-move-one"' in html
        assert ">Move<" in html
        assert ">Fail Note<" in html  # renamed from "Final Notes" 2026-09-03
        assert "Cracked hinge, keeps rebooting" in html
        assert 'id="fqcAssignBktModal"' not in html
        assert 'class="fqc-assign-bkt' not in html
        assert barcode in html
        assert bucket_name in html
        # ...and its Move button still routes it.
        r = _move_failed(app_client, [barcode])
        assert r.json()["moved"] == [barcode]
        assert _device_state(barcode)["stage"] == "l1"
    finally:
        _cleanup_device(barcode)


def test_move_hardware_reason_goes_to_l1_with_resolved_engineer(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVL1{suffix}"
    bucket_name = f"ITestMvL1{suffix}"
    eng_username, _ = make_user("l1_engineer")

    _seed_device_at_final_qc(barcode)
    _seed_workorder(barcode, "l1", eng_username)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Hardware", bucket_name)

        # The Fail decision routes the tag on by itself (2026-11-16, no Fail Hold wait).

        after = _device_state(barcode)
        assert after["stage"] == "l1"
        assert after["assigned_username"] == eng_username
    finally:
        _cleanup_device(barcode)


def test_move_software_reason_goes_to_stress_test_when_engineer_resolved(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVQC{suffix}"
    bucket_name = f"ITestMvQC{suffix}"
    eng_username, _ = make_user("qc_inspector")

    _seed_device_at_final_qc(barcode)
    _seed_workorder(barcode, "qc", eng_username)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Software", bucket_name)

        # The Fail decision routes the tag on by itself (2026-11-16, no Fail Hold wait).

        after = _device_state(barcode)
        assert after["stage"] == "qc_check"
        assert after["assigned_username"] == eng_username
    finally:
        _cleanup_device(barcode)


def test_move_cosmetic_reason_goes_to_cosmetic_received_when_engineer_resolved(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVCOS{suffix}"
    bucket_name = f"ITestMvCos{suffix}"
    eng_username, _ = make_user("cosmetic_manager")

    _seed_device_at_final_qc(barcode)
    _seed_workorder(barcode, "comp", eng_username)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Cosmetic", bucket_name)

        # The Fail decision routes the tag on by itself (2026-11-16, no Fail Hold wait).

        after = _device_state(barcode)
        assert after["stage"] == "cosmetic_received"
        assert after["assigned_username"] == eng_username
    finally:
        _cleanup_device(barcode)


def test_move_software_blank_engineer_still_moves_to_stress_test(app_client, make_user):  # noqa: F811
    """Software with no resolvable Engineer Name still moves to Stress Test
    (qc_check), unassigned — only Hardware has a "park in Tag Number
    Allocation" fallback."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVNOENG{suffix}"
    bucket_name = f"ITestMvNoEng{suffix}"

    _seed_device_at_final_qc(barcode)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Software", bucket_name)

        # The Fail decision routes the tag on by itself (2026-11-16, no Fail Hold wait).

        after = _device_state(barcode)
        assert after["stage"] == "qc_check"
        assert after["assigned_username"] == "None"
    finally:
        _cleanup_device(barcode)


def test_move_cosmetic_blank_engineer_still_moves_to_cosmetic_received(app_client, make_user):  # noqa: F811
    """Cosmetic with no resolvable Engineer Name still moves to Cosmetic
    Received, unassigned — same "no fallback" rule as Software."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVCOSNOENG{suffix}"
    bucket_name = f"ITestMvCosNoEng{suffix}"

    _seed_device_at_final_qc(barcode)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Cosmetic", bucket_name)

        # The Fail decision routes the tag on by itself (2026-11-16, no Fail Hold wait).

        after = _device_state(barcode)
        assert after["stage"] == "cosmetic_received"
    finally:
        _cleanup_device(barcode)


def test_move_hardware_blank_engineer_parks_in_trc_production(app_client, make_user):  # noqa: F811
    """Hardware is the ONLY reason with a "park in Tag Number Allocation"
    fallback when no engineer resolves — Software/Cosmetic never do this."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVHWNOENG{suffix}"
    bucket_name = f"ITestMvHwNoEng{suffix}"

    _seed_device_at_final_qc(barcode)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Hardware", bucket_name)

        # The Fail decision routes the tag on by itself (2026-11-16, no Fail Hold wait).

        after = _device_state(barcode)
        assert after["stage"] == "trc_production"
    finally:
        _cleanup_device(barcode)


def test_bulk_move_routes_each_tag_independently_even_sharing_a_bucket_name(app_client, make_user):  # noqa: F811
    """Two tags sharing one Bucket Name but different Failure Reasons — Bulk
    Move must route each to its OWN destination, unlike the old bucket-wide
    Assign that moved every tag in a bucket to one shared destination."""
    suffix = uuid.uuid4().hex[:6]
    barcode_1 = f"ITBULK1{suffix}"
    barcode_2 = f"ITBULK2{suffix}"
    bucket_name = f"ITestBulk{suffix}"
    eng_username, _ = make_user("l1_engineer")

    _seed_device_at_final_qc(barcode_1)
    _seed_workorder(barcode_1, "l1", eng_username)
    _seed_device_at_final_qc(barcode_2)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode_1, "Hardware", bucket_name)
        _submit_fail(app_client, barcode_2, "Software", bucket_name)

        # Each Fail routes its own tag on straight away (2026-11-16).

        assert _device_state(barcode_1)["stage"] == "l1"
        assert _device_state(barcode_1)["assigned_username"] == eng_username
        # barcode_2 (Software, no resolvable engineer) still moves to Stress
        # Test — a different destination from barcode_1, proving each tag
        # routes independently.
        assert _device_state(barcode_2)["stage"] == "qc_check"
    finally:
        _cleanup_device(barcode_1)
        _cleanup_device(barcode_2)


def test_move_skips_tag_no_longer_in_fail_hold_and_reports_it(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMVSKIP{suffix}"
    bucket_name = f"ITestMvSkip{suffix}"

    _seed_device_at_final_qc(barcode)
    admin_username, admin_password = make_user("admin")
    _login(app_client, admin_username, admin_password)
    try:
        _submit_fail(app_client, barcode, "Software", bucket_name)
        # The Fail already routed it on to qc_check (2026-11-16).

        # Already moved on to qc_check — a second Move on the same barcode
        # must be reported as skipped, not silently re-moved or errored.
        second = _move_failed(app_client, [barcode])
        assert second.status_code == 200, second.text[:400]
        body = second.json()
        assert body["moved"] == []
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["barcode"] == barcode
    finally:
        _cleanup_device(barcode)
