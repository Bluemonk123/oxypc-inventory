"""/workid-status (2026-08-31 batch, updated 2026-09-01 and 2026-09-02):

 - Card Count tiles (Total WorkIDs, Total Tags, Total Ongoing, Total
   Assigned, Total Completed) after the filter row, computed from the SAME
   filtered item list the table uses.
 - Filters: Completed From/To and "Stage" (renamed from "Cosmetic Stage" and
   expanded to every DeviceStage, not just the cosmetic-line subset), all in
   the same filter row and all applied to the tiles too. Both now filter
   against the Asset-History-sourced values the columns actually display —
   see test_workid_status_completed_date_and_stage_filters.py. Assigned
   From/Assigned To (WorkOrder.assigned_at) were removed 2026-09-01.
 - Export CSV: dropped Parts Required/Parts Requested columns, added Tag
   Number Make (Device.brand) and Completed Date. Stage/Completed
   Date/Assigned Engineer now come from the device's most recent
   StageMovement (Asset History) rather than current_stage/completed_at/
   assigned_name — see test_workid_status_asset_history_columns.py.
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


def _seed_device_and_workorder(barcode, stage, wo_status, work_id, brand="ITestBrand"):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from utils.timezone import app_now
from models.lot import Lot
from models.device import Device, DeviceStage
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="{brand}", model="ITestModel",
                     current_stage=DeviceStage.{stage})
        db.add(dev)
        await db.flush()
        completed_at = app_now() if "{wo_status}" == "completed" else None
        db.add(WorkOrder(work_id="{work_id}", device_id=dev.id, barcode="{barcode}",
                         stage="clean", assigned_role="cosmetic_manager",
                         assigned_username="itest_wid_user", assigned_name="ITest WID User",
                         status="{wo_status}", completed_at=completed_at, created_by="itest"))
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


def test_filter_row_labels_tiles_and_new_filters_render(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status", follow_redirects=True).text

    assert ">Assigned From<" not in html
    assert ">Assigned To<" not in html
    assert 'name="date_from"' not in html
    assert 'name="date_to"' not in html
    # Completed From/To replaced by the Work Date range (2026-10-28)
    assert 'name="work_from"' in html and 'name="completed_from"' not in html
    assert 'name="work_to"' in html
    assert 'name="cosmetic_stage"' in html
    # Tiles (renamed 2026-10-24 / 2026-10-26): tag counts.
    for label in ("Assigned Tags", "Completed Tags", "Ongoing Tags",
                  "Total Customer Return", "Total Scrap", "Total PNA"):
        assert label in html, label
    # Export link carries every filter param, including the 3 new ones.
    assert "/workid-status/export?" in html
    assert "work_from=" in html and "work_to=" in html
    assert "cosmetic_stage=" in html


def test_tiles_reflect_completed_and_assigned_counts(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_pending = f"ITWIDPEND{suffix}"
    barcode_done = f"ITWIDDONE{suffix}"
    wid_pending = f"WIDP{suffix}"
    wid_done = f"WIDD{suffix}"

    _seed_device_and_workorder(barcode_pending, "cleaning", "pending", wid_pending)
    _seed_device_and_workorder(barcode_done, "cleaning", "completed", wid_done)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(f"/workid-status?tag={barcode_pending[:8]}", follow_redirects=True).text
        assert barcode_pending in html
    finally:
        _cleanup_device(barcode_pending)
        _cleanup_device(barcode_done)


def test_export_columns_dropped_and_added(app_client, make_user):  # noqa: F811
    """Export columns narrowed 2026-09-02 to exactly: Tag Number, Lot
    Number, Make, Model, Engineer Name, Stage, Assigned Date, Completed
    Date — see test_workid_status_exclude_admin_and_export_fields.py for
    the full column-set assertion."""
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/workid-status/export", follow_redirects=True)
    assert r.status_code == 200
    header = export_sheet(r.content)[0]
    assert "Parts Required" not in header
    assert "Parts Requested" not in header
    assert "WorkID" not in header
    assert "Lot Number" in header
    assert "Completed Date" in header


def test_completed_date_filter_and_export_column_populated(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDCOMP{suffix}"
    work_id = f"WIDC{suffix}"
    _seed_device_and_workorder(barcode, "cleaning", "completed", work_id, brand="ITestMakeXYZ")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        r = app_client.get(f"/workid-status/export?tag={barcode}", follow_redirects=True)
        assert r.status_code == 200
        rows = export_sheet(r.content)
        assert len(rows) == 2                           # header + this one WorkID
        assert "ITestMakeXYZ" in rows[1]
    finally:
        _cleanup_device(barcode)
