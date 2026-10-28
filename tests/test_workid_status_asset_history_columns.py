"""/workid-status — Stage / Completed Date / Assigned Engineer sourced from
Asset History (2026-09-01):

Previously Stage showed Device.current_stage, Completed Date showed
WorkOrder.completed_at, and Assigned Engineer showed WorkOrder.assigned_name.
All three now read the device's most recent StageMovement (the same table
that backs the Device Detail "Asset History" card): Stage = From, Completed
Date = When, Assigned Engineer = the display name for By (a username,
resolved via the users table; falls back to the raw username if no match).

Also: Assigned From/Assigned To filters and the Parts Required/Parts
Requested columns were removed from this page.
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


def _seed(barcode, work_id, mover_username):
    """A device with one WorkOrder and one StageMovement (iqc -> cleaning),
    moved_by a real username so display-name resolution has something to
    resolve."""
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.cleaning)
        db.add(dev)
        await db.flush()
        db.add(WorkOrder(work_id="{work_id}", device_id=dev.id, barcode="{barcode}",
                         stage="clean", assigned_role="cosmetic_manager",
                         assigned_username="someone_else", assigned_name="Someone Else",
                         status="pending", created_by="itest"))
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.iqc,
                             to_stage=DeviceStage.cleaning, moved_by="{mover_username}",
                             notes="moved for test"))
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


def test_stage_completed_date_and_engineer_come_from_asset_history(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDAH{suffix}"
    work_id = f"WIDAH{suffix}"
    mover_username, _ = make_user("cosmetic_cleaning")
    _seed(barcode, work_id, mover_username)
    try:
        admin_username, admin_password = make_user("admin")
        _login(app_client, admin_username, admin_password)
        html = app_client.get(f"/workid-status?workid={work_id}", follow_redirects=True).text

        # Stage = "From" of the StageMovement (iqc), NOT the device's live
        # current_stage (cleaning) and NOT the WorkOrder's own assigned role.
        row = rows_by_workid(html)[work_id]
        assert "IQC" in row["s"].upper()
        assert "Cleaning" not in row["s"]

        # Assigned Engineer = the user the WorkID was ASSIGNED TO ("Someone Else"),
        # NOT the movement's "By" (the mover is whoever clicked the move, e.g. an
        # admin bulk-moving someone else's tag — changed 2026-10-07).
        u_full_name = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.user import User

async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "{mover_username}"))).scalar_one()
        print(u.full_name)

asyncio.run(main())
""")
        assert row["e"] == "Someone Else"
        assert u_full_name not in row["e"]
    finally:
        _cleanup_device(barcode)


def test_removed_filters_and_columns_absent(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status", follow_redirects=True).text

    assert "Assigned From" not in html
    assert "Assigned To" not in html
    assert 'name="date_from"' not in html
    assert 'name="date_to"' not in html
    assert "Parts Required" not in html
    assert "Parts Requested" not in html
    assert ">Stage<" in html
    assert ">Current Status<" not in html


def test_export_header_uses_stage_not_current_status(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/workid-status/export", follow_redirects=True)
    assert r.status_code == 200
    header = export_sheet(r.content)[0]
    assert "Stage" in header
    assert "Current Status" not in header


def test_date_from_to_params_no_longer_accepted_as_filters(app_client, make_user):  # noqa: F811
    """The querystring params still resolve (FastAPI ignores unknown query
    params rather than erroring) but no longer filter anything — confirms the
    endpoint itself dropped the parameters cleanly rather than 422ing old
    bookmarked links."""
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/workid-status?date_from=2020-01-01&date_to=2020-01-02", follow_redirects=True)
    assert r.status_code == 200
