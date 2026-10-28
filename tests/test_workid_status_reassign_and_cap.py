"""WorkID Status: reassignment completes the previous assignee's WorkID, the page
shows the ASSIGNED user, and a Completed From/To filter is not cut off by the row cap.

Found 2026-10-07 from three reports:
  * a WorkID assigned to Saroj showed the admin who bulk-moved the tag;
  * a WorkID Sanjeet completed on 6 Oct was missing (the 3,000-row cap keeps the
    most recently ASSIGNED WorkIDs and the date filter only saw those);
  * Change Engineer / Assign created the new engineer's WorkID but left the
    previous engineer's "pending", so it never got a Completed Date.
"""
import asyncio
import inspect
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


# ── pure: the shared helper + wiring ─────────────────────────────────────────
class _RecordingDB:
    def __init__(self):
        self.stmts = []

    async def execute(self, stmt, *a, **k):
        self.stmts.append(stmt)
        return type("R", (), {"rowcount": 2})()


def test_close_helper_updates_only_open_non_asgn_work_orders():
    from services.work_order_close import close_open_work_orders
    db = _RecordingDB()
    n = asyncio.run(close_open_work_orders(db, uuid.uuid4()))
    assert n == 2
    sql = str(db.stmts[0].compile())
    assert sql.startswith("UPDATE work_orders SET")
    assert "status" in sql and "completed_at" in sql
    # "open" = status not in the closed set (completed / reassigned), via wo_open()
    assert "work_orders.status NOT IN" in sql and "work_orders.stage !=" in sql
    assert "work_orders.device_id =" in sql


def test_every_reassignment_path_closes_the_previous_work_order_first():
    from routers import buckets, stock
    for fn in (buckets.assign_device, buckets.bulk_assign_devices_l1l2, stock.change_engineer):
        src = inspect.getsource(fn)
        # (2026-10-17: may pass keep_id= for a reused STRS- WorkID. 2026-10-23:
        # Change Engineer marks the old WorkID "reassigned" rather than
        # completed, via reassign_open_work_orders.)
        closer = ("close_open_work_orders" if "close_open_work_orders(db, device.id" in src
                  else "reassign_open_work_orders")
        assert f"{closer}(db, device.id" in src, fn.__name__
        assert src.index(closer) < src.index("WorkOrder("), fn.__name__
    # bucket / Final-QC-fail assignment (shared helper)
    src = inspect.getsource(buckets)
    assert src.count("close_open_work_orders(db, device.id") >= 3


# ── DB-backed: engineer shown is the assignee; date filter beats the cap ─────
def _seed(barcode, rows, mover_username):
    """rows: list of (work_id, assigned_username, assigned_name, assigned_at, completed_at|None)."""
    _run(f"""
import asyncio, sys
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder

ROWS = {rows!r}

def dt(v):
    return datetime.fromisoformat(v) if v else None

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.ready_to_sale)
        db.add(dev)
        await db.flush()
        for wid, uname, name, a_at, c_at in ROWS:
            db.add(WorkOrder(work_id=wid, device_id=dev.id, barcode="{barcode}", stage="l1",
                             assigned_role="l1_engineer", assigned_username=uname, assigned_name=name,
                             assigned_at=dt(a_at), completed_at=dt(c_at),
                             status="completed" if c_at else "pending", created_by="itest"))
            if c_at:
                db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.l1,
                                     to_stage=DeviceStage.ready_to_sale, moved_by="{mover_username}",
                                     moved_at=dt(c_at), notes="Bulk Customise modal - bulk Move to Stage"))
        await db.commit()

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
            for m in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_page_shows_the_assignee_not_the_user_who_moved_the_tag(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode, wid = f"ITRA{suffix}", f"WRA{suffix}"
    mover, _ = make_user("admin")
    _seed(barcode, [(wid, "saroj_test", "Saroj Test", "2026-01-02T10:00:00", "2026-01-03T14:57:13")], mover)
    try:
        admin, pw = make_user("admin")
        _login(app_client, admin, pw)
        html = app_client.get(f"/workid-status?workid={wid}", follow_redirects=True).text
        row = rows_by_workid(html)[wid]
        assert row["e"] == "Saroj Test"                 # assigned engineer, with a completed date
        assert row["cd"] == "03-01-2026"
    finally:
        _cleanup(barcode)


def test_completed_date_filter_is_not_cut_off_by_the_row_cap(app_client, make_user, monkeypatch):  # noqa: F811
    import routers.workid_status as ws
    suffix = uuid.uuid4().hex[:6]
    barcode, prefix = f"ITRB{suffix}", f"WCAP{suffix}"
    mover, _ = make_user("admin")
    _seed(barcode, [
        (f"{prefix}A", "sanjeet_test", "Sanjeet Test", "2026-01-01T09:00:00", "2026-01-06T10:09:18"),  # old, completed
        (f"{prefix}B", "x_test", "X Test", "2026-02-01T09:00:00", None),                              # newer, open
        (f"{prefix}C", "y_test", "Y Test", "2026-02-02T09:00:00", None),                              # newer, open
    ], mover)
    try:
        monkeypatch.setattr(ws, "MAIN_ROW_CAP", 2)      # only the 2 most recently assigned fit
        admin, pw = make_user("admin")
        _login(app_client, admin, pw)
        html = app_client.get(f"/workid-status?workid={prefix}&completed_from=2026-01-06&completed_to=2026-01-06",
                              follow_redirects=True).text
        assert f"{prefix}A" in rows_by_workid(html)             # was missing before the date window went into SQL
        assert f"{prefix}B" not in rows_by_workid(html)
    finally:
        _cleanup(barcode)
