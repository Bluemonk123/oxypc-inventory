"""WorkID Status "Work Date" filter (2026-10-28): a day's work = WorkIDs
completed in the range plus WorkIDs with a PNA part marked in the range while
they were open. Open WorkIDs show the stage stored on them."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.workid_helpers import rows_by_workid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_work_date_completed_or_pna_marked(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tag = f"ITWD{sfx}"
    done, pna, idle, old = (f"WD{c}{sfx}" for c in "ABCD")
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder
from models.pna_part import DevicePNAPart
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=DeviceStage.l1)
        db.add(d); await db.flush()
        D = lambda day, h: datetime(2026, 10, day, h, 0)
        def wo(wid, st, a, c=None, who="wd_eng"):
            db.add(WorkOrder(work_id=wid, device_id=d.id, barcode="{tag}", stage="l1", status=st,
                             created_at=a, assigned_at=a, completed_at=c,
                             assigned_username=who, assigned_name=who))
        wo("{old}", "completed", D(25, 10), D(25, 11))      # outside the day
        wo("{done}", "completed", D(27, 9), D(27, 10))      # completed on the day
        wo("{pna}", "pending", D(27, 12))                   # PNA marked on the day
        wo("{idle}", "pending", D(26, 12), who="other_eng") # open, someone else's, nothing on the day
        db.add(StageMovement(device_id=d.id, from_stage=DeviceStage.qc_check, to_stage=DeviceStage.l1,
                             moved_at=D(27, 12), moved_by="t"))
        db.add(DevicePNAPart(device_id=d.id, barcode="{tag}", part_name="Screen", source="l1",
                             marked_by="wd_eng", marked_at=D(27, 13), is_active=True))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get(f"/workid-status?tag={tag}&work_from=2026-10-27&work_to=2026-10-27&show_reassigned=1").text
        rows = rows_by_workid(html)
        assert set(rows) == {done, pna}
        assert rows[pna]["s"] == "L1/L2 Repair"          # open: its own stage, not "Stress Test"
        allrows = rows_by_workid(app_client.get(f"/workid-status?tag={tag}&show_reassigned=1").text)
        assert {done, pna, idle, old} <= set(allrows)
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode = :b"
        await db.execute(text(f"delete from device_pna_parts where device_id in ({{ids}})"), {{"b": "{tag}"}})
        await db.execute(text(f"delete from stage_movements where device_id in ({{ids}})"), {{"b": "{tag}"}})
        await db.execute(text(f"delete from work_orders where device_id in ({{ids}})"), {{"b": "{tag}"}})
        await db.execute(text("delete from devices where barcode = :b"), {{"b": "{tag}"}})
        await db.commit()
asyncio.run(main())
""")
