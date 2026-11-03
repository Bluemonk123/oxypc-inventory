"""Cosmetic Pass credit (2026-11-03): a tag's last Cosmetic WorkID (Water
Sanding) closed the day before it moved Cosmetic Completed -> Final QC. With a
Work Date range of the move day, the Cosmetic tile counts the Pass, the Tag
Summary sheet carries it on that WorkID (added as a row, it closed before the
range) and the Staff Summary credits its engineer — not the supervisor who
moved it."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_pass_credited_to_last_cosmetic_workid_before_range(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tag, wid = f"CCR{sfx}", f"CCRW{sfx}"
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
        d = Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=DeviceStage.final_qc)
        db.add(d); await db.flush()
        db.add(WorkOrder(work_id="{wid}", device_id=d.id, barcode="{tag}", stage="water", status="completed",
                         created_at=datetime(2026, 10, 26, 9, 0), assigned_at=datetime(2026, 10, 26, 9, 0),
                         completed_at=datetime(2026, 10, 26, 11, 0),
                         assigned_username="ccr_eng", assigned_name="ccr_eng"))
        db.add(StageMovement(device_id=d.id, from_stage=DeviceStage.qc_check,
                             to_stage=DeviceStage.cosmetic_received, moved_at=datetime(2026, 10, 25, 10, 0), moved_by="ccr_qc"))
        db.add(StageMovement(device_id=d.id, from_stage=DeviceStage.water_sanding,
                             to_stage=DeviceStage.cosmetic_completed, moved_at=datetime(2026, 10, 26, 11, 0), moved_by="ccr_eng"))
        db.add(StageMovement(device_id=d.id, from_stage=DeviceStage.cosmetic_completed,
                             to_stage=DeviceStage.final_qc, moved_at=datetime(2026, 10, 27, 15, 0), moved_by="ccr_sup"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        import re
        html = app_client.get(f"/workid-status?tag={tag}&work_from=2026-10-27&work_to=2026-10-27").text
        cos = html.split('data-group="cosmetic"', 1)[1].split('data-group="final_qc"', 1)[0]
        got = dict((k, v) for v, k in re.findall(r'data-server="(\d+)">\d+</span><span class="wid-stage-sub">(Pass|Fail)', cos))
        assert got == {"Pass": "1", "Fail": "0"}, got
        def lines(day):
            h = app_client.get(f"/workid-status?tag={tag}&work_from={day}&work_to={day}").text
            c = h.split('data-group="cosmetic"', 1)[1].split('data-group="final_qc"', 1)[0]
            return dict((k, v) for v, k in re.findall(r'data-server="(\d+)">\d+</span><span class="wid-stage-sub">([^<]+)', c))
        # Tags Assigned = arrived in Cosmetic Received, Tags Completed = moved
        # into Cosmetic Completed, in the range (2026-11-03).
        assert lines("2026-10-25")["Received"] == "1" and lines("2026-10-25")["Tags Completed"] == "0"
        d26 = lines("2026-10-26")
        assert d26["Received"] == "0" and d26["Tags Completed"] == "1", d26
        d27 = lines("2026-10-27")
        assert d27["Received"] == "0" and d27["Tags Completed"] == "0" and d27["Pass"] == "1", d27
        from tests.workid_helpers import export_sheet
        x = app_client.get("/workid-status/export?engineer=ccr_eng&work_from=2026-10-27&work_to=2026-10-27")
        rows = export_sheet(x.content)
        h = rows[0]
        mine = [r for r in rows[1:] if r[0] == tag]
        assert len(mine) == 1 and mine[0][h.index("Cosmetic Result")] == "Pass", mine
        assert str(mine[0][h.index("Status")]).startswith("Completed (before")
        staff = export_sheet(x.content, "Staff Summary")
        sh = staff[0]
        eng = [r for r in staff if r[0] == "ccr_eng"]
        assert eng and eng[0][sh.index("Cosmetic Pass")] == 1, staff
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode = :t"
        await db.execute(text(f"delete from stage_movements where device_id in ({{ids}})"), {{"t": "{tag}"}})
        await db.execute(text(f"delete from work_orders where device_id in ({{ids}})"), {{"t": "{tag}"}})
        await db.execute(text("delete from devices where barcode = :t"), {{"t": "{tag}"}})
        await db.commit()
asyncio.run(main())
""")
