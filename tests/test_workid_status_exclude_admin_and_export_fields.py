"""/workid-status — "Exclude Admin" filter + narrowed CSV export (2026-09-02):

- New "Exclude Admin" checkbox (exclude_admin query param) drops rows whose
  engineer resolves to an admin-role User, checked by the underlying
  username (StageMovement.moved_by / WorkOrder.assigned_username), not a
  string match on the rendered display name.
- CSV export columns changed to exactly: Tag Number, Lot Number, Make,
  Model, Engineer Name, Stage, Assigned Date, Completed Date. Lot Number is
  new — Device.lot_id -> Lot.lot_number, looked up once for every device in
  the filtered item list.

2026-09-24: test_exclude_admin_drops_rows_moved_by_an_admin_user and
test_export_columns_match_the_requested_set removed — both seeded only a
bare StageMovement (no WorkOrder) and relied on the since-removed "backfill"
mechanism (routers/workid_status.py) to make it visible on the page at all.
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


def _seed_movement(barcode, mover_username, moved_at_iso, from_stage="cleaning", to_stage="putty"):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from datetime import datetime
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{to_stage})
        db.add(dev)
        await db.flush()
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.{from_stage},
                             to_stage=DeviceStage.{to_stage}, moved_by="{mover_username}",
                             moved_at=datetime.fromisoformat("{moved_at_iso}")))
        await db.commit()
        print(lot.lot_number)

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


def test_exclude_admin_keeps_rows_moved_by_a_non_admin_user(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITWIDEXB{suffix}"
    mover_username, _ = make_user("cosmetic_cleaning")
    admin_username, admin_password = make_user("admin")
    _seed_movement(barcode, mover_username, "2026-08-20T11:00:00")
    try:
        _login(app_client, admin_username, admin_password)
        html = app_client.get(f"/workid-status?tag={barcode}&exclude_admin=1", follow_redirects=True).text
        assert barcode in html
    finally:
        _cleanup_device(barcode)


def test_exclude_admin_param_threaded_through_export_link(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status?exclude_admin=1", follow_redirects=True).text
    assert "exclude_admin=1" in html
    assert 'id="excludeAdminChk"' in html
    assert "checked" in html.split('id="excludeAdminChk"', 1)[1].split(">", 1)[0]


def test_exclude_admin_ticked_by_default_and_no_show_reassigned(app_client, make_user):  # noqa: F811
    """2026-11-22 (Pankaj): Exclude Admin is ticked when the page opens; the
    form's hidden "0" unticks it; the Show Reassigned checkbox is gone."""
    username, password = make_user("admin")
    _login(app_client, username, password)

    def ticked(html):
        return "checked" in html.split('id="excludeAdminChk"', 1)[1].split(">", 1)[0]
    fresh = app_client.get("/workid-status").text
    assert ticked(fresh) and "exclude_admin=1" in fresh
    assert 'name="exclude_admin" value="0"' in fresh
    assert "showReassignedChk" not in fresh and "Show Reassigned" not in fresh
    off = app_client.get("/workid-status?exclude_admin=0").text
    assert not ticked(off) and "exclude_admin=&" in off
    on = app_client.get("/workid-status?exclude_admin=0&exclude_admin=1").text   # hidden 0 + ticked box
    assert ticked(on)
