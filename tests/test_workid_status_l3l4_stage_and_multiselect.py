"""/workid-status — 2026-09-15:

- Stage filter converted from a single-select to a multiselect dropdown
  (templates/_multiselect_filter.html, comma-separated `cosmetic_stage`),
  matching the convention already used on /devices. Selecting several
  stages at once must OR them together, not require an exact match.
- "L3/L4 WorkIDs not showing": routers/repair.py's request_l3l4 and
  l1l2_complete_to_stress create "L3L4-"/"STRS-" prefixed WorkOrders to make
  an L3/L4 repair or Stress Test hand-off visible on this page, but neither
  that flow nor l3l4_start/l3l4_complete/l3l4_scrap ever writes a
  StageMovement — the device's current_stage isn't touched by L3/L4 at all.
  The old Asset-History matching therefore fell back to the device's
  unrelated latest movement (whatever stage it passed through before ever
  reaching L1), so filtering for "L3 Repair" found nothing and the Completed
  Date column showed the wrong timestamp. Fixed by reading Stage/Completed
  Date/Engineer directly off the WorkOrder for these two prefixes instead of
  matching a StageMovement that was never written.
- A cap (MAIN_ROW_CAP) was added to the previously-unbounded base WorkOrder
  query — the unfiltered default view is the page's slowest case.
"""
import pathlib
import subprocess
import sys
import uuid
from datetime import datetime

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.workid_helpers import export_sheet, page_rows, rows_by_workid  # noqa: F401

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def _seed_l3l4_workorder(barcode, work_id, completed_at_iso=None):
    """A device whose only real StageMovement is a stale, unrelated one
    (trc_production -> l1, months ago) plus an "L3L4-" WorkOrder — mirrors
    request_l3l4()'s actual behavior: no StageMovement is ever written for
    the L3/L4 hand-off itself."""
    completed_line = (
        f'wo.completed_at = datetime.fromisoformat("{completed_at_iso}")\n        wo.status = "completed"'
        if completed_at_iso else ""
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
                     current_stage=DeviceStage.l1)
        db.add(dev)
        await db.flush()
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.trc_production,
                             to_stage=DeviceStage.l1, moved_by="itest_old",
                             moved_at=datetime.fromisoformat("2020-01-01T10:00:00")))
        wo = WorkOrder(work_id="{work_id}", device_id=dev.id, barcode="{barcode}",
                       stage="l3", assigned_role="l3_engineer",
                       assigned_username="itest_l3l4_eng", assigned_name="ITest L3L4 Engineer",
                       status="pending", requested_by_name="itest", created_by="itest")
        {completed_line}
        db.add(wo)
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


def test_l3l4_workorder_shows_l3_repair_stage_not_stale_movement(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITL3L4{suffix}"
    work_id = f"L3L4-9{suffix[:4]}"
    _seed_l3l4_workorder(barcode, work_id)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)

        # Filtering Stage=l3 (L3/L4 Repair) must find it — previously showed
        # "trc_production" (the stale unrelated movement) instead.
        html = app_client.get(f"/workid-status?workid={work_id}&cosmetic_stage=l3",
                              follow_redirects=True).text
        row = rows_by_workid(html)[work_id]
        assert row["s"] == "L3/L4 Repair"
        assert "TRC Production" not in row["s"]
    finally:
        _cleanup_device(barcode)


def test_l3l4_completed_date_reads_workorder_not_stale_movement(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITL3L4B{suffix}"
    work_id = f"L3L4-8{suffix[:4]}"
    # completed_at is recent; the device's only real StageMovement is dated
    # 2020 — Completed Date must reflect the WorkOrder's own field.
    _seed_l3l4_workorder(barcode, work_id, completed_at_iso="2026-09-15T12:00:00")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)

        html_in_range = app_client.get(
            f"/workid-status?workid={work_id}&completed_from=2026-09-01&completed_to=2026-09-30",
            follow_redirects=True).text
        assert work_id in rows_by_workid(html_in_range)

        html_out_of_range = app_client.get(
            f"/workid-status?workid={work_id}&completed_from=2020-01-01&completed_to=2020-01-31",
            follow_redirects=True).text
        assert work_id not in rows_by_workid(html_out_of_range)
    finally:
        _cleanup_device(barcode)


def test_stage_multiselect_filter_ors_selected_stages(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITL3L4C{suffix}"
    work_id = f"L3L4-7{suffix[:4]}"
    _seed_l3l4_workorder(barcode, work_id)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)

        # Two stages selected, one of which (l3) matches — must still show.
        html = app_client.get(f"/workid-status?workid={work_id}&cosmetic_stage=iqc,l3",
                              follow_redirects=True).text
        assert work_id in rows_by_workid(html)

        # Neither selected stage matches — must not show.
        html_none = app_client.get(f"/workid-status?workid={work_id}&cosmetic_stage=iqc,sold",
                                   follow_redirects=True).text
        assert work_id not in rows_by_workid(html_none)
    finally:
        _cleanup_device(barcode)


def test_stage_filter_is_a_multiselect_checkbox_dropdown():
    html = (pathlib.Path(ROOT) / "templates" / "workid_status" / "list.html").read_text(encoding="utf-8")
    assert "ms.multiselect('cosmetic_stage'" in html
    assert '<select name="cosmetic_stage"' not in html


def test_main_query_has_a_row_cap_with_truncation_notice():
    src = (pathlib.Path(ROOT) / "routers" / "workid_status.py").read_text(encoding="utf-8")
    assert "MAIN_ROW_CAP" in src
    # The page passes row_cap=MAIN_ROW_CAP; Export passes None (every row).
    assert "row_cap=MAIN_ROW_CAP" in src
    assert "stmt = stmt.limit(row_cap)" in src
    html = (pathlib.Path(ROOT) / "templates" / "workid_status" / "list.html").read_text(encoding="utf-8")
    assert "main_truncated" in html
