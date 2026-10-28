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
    done, pna, idle, old, late = (f"WD{c}{sfx}" for c in "ABCDE")
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
        # PNA marked on the day, finished the day after: listed, not counted as completed that day
        d2 = Device(id=uuid.uuid4(), barcode="{tag}X", lot_id=lot, current_stage=DeviceStage.qc_check)
        db.add(d2); await db.flush()
        db.add(WorkOrder(work_id="{late}", device_id=d2.id, barcode="{tag}X", stage="l1", status="completed",
                         created_at=D(27, 9), assigned_at=D(27, 9), completed_at=D(28, 10),
                         assigned_username="wd_eng", assigned_name="wd_eng"))
        db.add(DevicePNAPart(device_id=d2.id, barcode="{tag}X", part_name="RAM", source="l1",
                             marked_by="wd_eng", marked_at=D(27, 11), is_active=False))
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
        assert set(rows) == {done, pna, late}
        # Completed counts only what was completed on the day
        assert rows[done]["st"] == "completed" and rows[late]["st"] == "in_progress"
        import re
        m = re.search(r'pm-tile-num"[^>]*>(\d+)</div>\s*<div class="pm-tile-lbl">Completed Tags', html)
        assert m and m.group(1) == "1"
        assert 'name="completed_from"' not in html          # old date range hidden
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
        ids = "select id from devices where barcode in (:b, :bx)"
        await db.execute(text(f"delete from device_pna_parts where device_id in ({{ids}})"), {{"b": "{tag}", "bx": "{tag}X"}})
        await db.execute(text(f"delete from stage_movements where device_id in ({{ids}})"), {{"b": "{tag}", "bx": "{tag}X"}})
        await db.execute(text(f"delete from work_orders where device_id in ({{ids}})"), {{"b": "{tag}", "bx": "{tag}X"}})
        await db.execute(text("delete from devices where barcode in (:b, :bx)"), {{"b": "{tag}", "bx": "{tag}X"}})
        await db.commit()
asyncio.run(main())
""")


def test_stress_tile_pass_and_fail(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tp, tf = f"ITSP{sfx}", f"ITSF{sfx}"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        a, c = datetime(2026, 10, 27, 9, 0), datetime(2026, 10, 27, 11, 0)
        for bc, frm, nxt, wid, code in (
                ("{tp}", DeviceStage.qc_check, DeviceStage.cosmetic_received, "STRS-P{sfx}", "qc"),
                ("{tf}", DeviceStage.qc_check, DeviceStage.l1, "STRS-F{sfx}", "qc"),
                ("{tp}Q", DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, "FQP{sfx}", "fqc"),
                ("{tf}Q", DeviceStage.final_qc, DeviceStage.final_qc_fail_hold, "FQF{sfx}", "fqc")):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=nxt)
            db.add(d); await db.flush()
            db.add(WorkOrder(work_id=wid, device_id=d.id, barcode=bc, stage=code, status="completed",
                             created_at=a, assigned_at=a, completed_at=c,
                             assigned_username="sp_eng", assigned_name="sp_eng"))
            db.add(StageMovement(device_id=d.id, from_stage=frm, to_stage=nxt, moved_at=c, moved_by="t"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get("/workid-status?engineer=sp_eng&work_from=2026-10-27&work_to=2026-10-27").text
        stress = html.split('data-group="stress"', 1)[1].split('data-group="cosmetic"', 1)[0]
        import re
        got = dict((k, v) for v, k in re.findall(r'data-server="(\d+)">\d+</span><span class="wid-stage-sub">(Pass|Fail)', stress))
        assert got == {"Pass": "1", "Fail": "1"}, got
        fqc = html.split('data-group="final_qc"', 1)[1][:3000]
        got = dict((k, v) for v, k in re.findall(r'data-server="(\d+)">\d+</span><span class="wid-stage-sub">(Pass|Fail)', fqc))
        assert got == {"Pass": "1", "Fail": "1"}, got
        assert 'name="workid"' not in html and 'name="tag"' not in html      # search boxes hidden
        from tests.workid_helpers import export_sheet
        x = app_client.get("/workid-status/export?engineer=sp_eng&work_from=2026-10-27&work_to=2026-10-27")
        tag_rows = export_sheet(x.content)
        qi = tag_rows[0].index("QC Result")
        assert sorted(r[qi] for r in tag_rows[1:]) == ["Fail", "Fail", "Pass", "Pass"]
        staff = export_sheet(x.content, "Staff Summary")
        h = staff[0]
        row = [r for r in staff if r[0] == "sp_eng"][0]
        assert [row[h.index(c)] for c in ("Stress Pass", "Stress Fail", "Final QC Pass", "Final QC Fail")] == [1, 1, 1, 1]
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in (:a, :b, :c, :d)"
        p = {{"a": "{tp}", "b": "{tf}", "c": "{tp}Q", "d": "{tf}Q"}}
        await db.execute(text(f"delete from stage_movements where device_id in ({{ids}})"), p)
        await db.execute(text(f"delete from work_orders where device_id in ({{ids}})"), p)
        await db.execute(text("delete from devices where barcode in (:a, :b, :c, :d)"), p)
        await db.commit()
asyncio.run(main())
""")
