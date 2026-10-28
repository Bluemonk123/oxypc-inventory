"""/workid-status — Completed Date filter fix + Stage filter expansion
(2026-09-02):

Completed From/To previously filtered WorkOrder.completed_at at the SQL
level, but the Completed Date COLUMN (since the 2026-09-01 Asset History
change) shows the device's latest StageMovement.moved_at instead — a
different field, so the filter silently narrowed against a value nobody
could see, and rows outside [from, to] on the *displayed* date could still
appear ("not applying properly"). Both Completed Date and the renamed
"Stage" filter (was "Cosmetic Stage", now offers every DeviceStage, not just
the cosmetic-line subset) now filter the SAME Asset-History-sourced values
the Stage/Completed Date columns display.

2026-09-19: Completed Date only shows a value once the WorkOrder is
genuinely completed (status="completed") — a still-open/pending WorkOrder
now shows blank Completed Date (see routers/workid_status.py's module
docstring), so the date-filter test's fixture must mark its WorkOrder
completed for the Completed Date column to have anything to filter on.
"""
import pathlib
import subprocess
import sys
import uuid
from datetime import timedelta

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.workid_helpers import export_sheet, page_rows, rows_by_workid  # noqa: F401

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def _seed(barcode, work_id, from_stage, to_stage, moved_at_iso, completed=True):
    # completed=True marks the WorkOrder itself done (status="completed",
    # completed_at=moved_at) -- required for the Completed Date column to
    # show anything at all under the 2026-09-19 per-WorkID behavior; pass
    # False to test the "still open" (blank Completed Date) case instead.
    completed_line = (
        f'wo.status = "completed"\n        wo.completed_at = datetime.fromisoformat("{moved_at_iso}")'
        if completed else ""
    )
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from datetime import datetime
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{to_stage})
        db.add(dev)
        await db.flush()
        wo = WorkOrder(work_id="{work_id}", device_id=dev.id, barcode="{barcode}",
                         stage="clean", assigned_role="cosmetic_manager",
                         assigned_username="itest_cdf", assigned_name="ITest CDF",
                         status="pending", created_by="itest")
        {completed_line}
        db.add(wo)
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.{from_stage},
                             to_stage=DeviceStage.{to_stage}, moved_by="itest_cdf",
                             moved_at=datetime.fromisoformat("{moved_at_iso}")))
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


def test_completed_date_filter_matches_the_displayed_column(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDCDF{suffix}"
    work_id = f"WIDCDF{suffix}"
    # Moved on a fixed, distant date — well outside "today".
    _seed(barcode, work_id, "iqc", "cleaning", "2020-01-15T10:00:00")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)

        # A range that INCLUDES the movement date must show the row.
        html_in = app_client.get(
            f"/workid-status?workid={work_id}&completed_from=2020-01-01&completed_to=2020-01-31",
            follow_redirects=True).text
        assert work_id in html_in

        # A range that EXCLUDES it must not show the row — checked via the
        # row's own id, not a bare "work_id in html" check, since the
        # WorkID search box always echoes back its own filter value
        # (value="{{ f_workid }}") regardless of what the table shows.
        html_out = app_client.get(
            f"/workid-status?workid={work_id}&completed_from=2021-01-01&completed_to=2021-01-31",
            follow_redirects=True).text
        assert work_id not in rows_by_workid(html_out)
    finally:
        _cleanup_device(barcode)


def test_stage_filter_matches_asset_history_from_value(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDSTG{suffix}"
    work_id = f"WIDSTG{suffix}"
    # From=iqc, To=cleaning — Stage column shows "From" (iqc), not the
    # device's live current_stage (cleaning).
    _seed(barcode, work_id, "iqc", "cleaning", "2026-01-01T10:00:00")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)

        html_iqc = app_client.get(f"/workid-status?workid={work_id}&cosmetic_stage=iqc",
                                  follow_redirects=True).text
        assert work_id in rows_by_workid(html_iqc)

        html_cleaning = app_client.get(f"/workid-status?workid={work_id}&cosmetic_stage=cleaning",
                                       follow_redirects=True).text
        assert work_id not in rows_by_workid(html_cleaning)
    finally:
        _cleanup_device(barcode)


def test_open_workorder_shows_blank_completed_date_but_running_aging(app_client, make_user):  # noqa: F811
    import re
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDOPEN{suffix}"
    work_id = f"WIO{suffix}"  # work_orders.work_id is VARCHAR(12)
    # Still open (completed=False) -- the tag is still with this engineer,
    # not yet handed off to another stage/assignee. WorkOrder.assigned_at
    # defaults to "now" (not seeded), so any "2020" text in the row can only
    # have leaked in from the StageMovement's moved_at via Completed Date.
    _seed(barcode, work_id, "iqc", "cleaning", "2020-01-15T10:00:00", completed=False)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(f"/workid-status?workid={work_id}", follow_redirects=True).text
        row = rows_by_workid(html)[work_id]

        # Aging: a running count (the page labels it "ongoing" while no
        # Completed Date is set), never blank.
        assert row["c"] == "" and row["cd"] == ""
        assert isinstance(row["d"], int) and row["d"] >= 0

        # Completed Date column itself stays blank -- the 2020 movement date
        # must not leak into the row at all (it previously did, via the
        # "device's latest movement as a best guess" fallback).
        import json as _json
        assert "2020" not in _json.dumps(row)
    finally:
        _cleanup_device(barcode)


def test_completed_date_filter_excludes_still_open_workorder(app_client, make_user):  # noqa: F811
    """The exact bug this batch fixes: before, a still-open WorkOrder's
    Completed Date fell back to the device's latest StageMovement, so a
    Completed Date range filter could wrongly match a tag that hasn't
    actually been completed at all."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDOPENF{suffix}"
    work_id = f"WIOF{suffix}"  # work_orders.work_id is VARCHAR(12)
    _seed(barcode, work_id, "iqc", "cleaning", "2020-01-15T10:00:00", completed=False)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(
            f"/workid-status?workid={work_id}&completed_from=2020-01-01&completed_to=2020-01-31",
            follow_redirects=True).text
        assert work_id not in rows_by_workid(html)
    finally:
        _cleanup_device(barcode)


def test_stage_dropdown_includes_non_cosmetic_stages(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status", follow_redirects=True).text
    assert ">Stage<" in html
    assert ">Cosmetic Stage<" not in html
    # Non-cosmetic stages ("iqc", "ready_to_sale") are offered too. "sold" is
    # deliberately left out of every stage dropdown since 2026-09-21
    # (models/device.py DROPDOWN_STAGES).
    assert 'value="ready_to_sale"' in html
    assert 'value="iqc"' in html
