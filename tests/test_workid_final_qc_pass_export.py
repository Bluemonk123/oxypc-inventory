"""WorkID Status -> Final QC Pass export (2026-11-05, Pankaj): tags whose
Final QC WorkID was completed as Pass (to Pass Hold / Ready for Sale) in the
Work Date range, in the Repair Summary layout — the same WorkIDs as the Final
QC tile's Pass. A tag passed twice is one row, Times Passed = 2; a Final QC
fail and a tag that only went to Ready for Sale from IQC are not in it."""
import io
import pathlib
import subprocess
import sys
import uuid
from datetime import date, timedelta

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_final_qc_pass_export(app_client, make_user):  # noqa: F811
    from openpyxl import load_workbook
    sfx = uuid.uuid4().hex[:6].upper()
    tp, tf, ti = f"ITFQ{sfx}P", f"ITFQ{sfx}F", f"ITFQ{sfx}I"
    _run(f"""
import asyncio, sys, uuid
from datetime import timedelta
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        now = app_now()
        devs = {{}}
        for bc in ("{tp}", "{tf}", "{ti}"):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                       device_type="Laptop", brand="Dell", model="Latitude 5490")
            db.add(d); devs[bc] = d
        await db.flush()
        def mv(bc, frm, to, at):
            db.add(StageMovement(device_id=devs[bc].id, from_stage=frm, to_stage=to, moved_by="t", moved_at=at))
        def wo(bc, n, at):
            db.add(WorkOrder(work_id=f"ITFQW{sfx}{{n}}", device_id=devs[bc].id, barcode=bc, stage="fqc",
                             status="completed", assigned_name="t", assigned_username="t",
                             assigned_at=at - timedelta(minutes=30), created_at=at - timedelta(minutes=30),
                             completed_at=at))
        a1, a2 = now - timedelta(hours=3), now - timedelta(hours=1)
        mv("{tp}", DeviceStage.l1, DeviceStage.final_qc, a1 - timedelta(hours=1))
        mv("{tp}", DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, a1); wo("{tp}", 1, a1)
        mv("{tp}", DeviceStage.final_qc_pass_hold, DeviceStage.final_qc, a2 - timedelta(minutes=40))
        mv("{tp}", DeviceStage.final_qc, DeviceStage.ready_to_sale, a2); wo("{tp}", 2, a2)
        mv("{tf}", DeviceStage.l1, DeviceStage.final_qc, a1 - timedelta(hours=1))
        mv("{tf}", DeviceStage.final_qc, DeviceStage.l1, a1); wo("{tf}", 3, a1)
        mv("{ti}", DeviceStage.iqc, DeviceStage.ready_to_sale, a1)
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        today = date.today().isoformat()
        assert "/workid-status/final-qc-pass" in app_client.get(
            "/workid-status", params={"work_from": today, "work_to": today}).text

        def export(**params):
            r = app_client.get("/workid-status/final-qc-pass", params=params)
            assert r.status_code == 200, r.text[:300]
            ws = load_workbook(io.BytesIO(r.content)).active
            head = [c.value for c in ws[1]]
            return ws.title, head, {x[0]: dict(zip(head, x)) for x in ws.iter_rows(min_row=2, values_only=True)}

        title, head, rows = export(tag=f"ITFQ{sfx}", work_from=today, work_to=today)
        assert title == "Final QC Pass"
        assert head[:3] == ["Tag Number", "GRN", "Invoice Number"]
        assert ["Final QC Pass Date", "Times Passed", "Passed To", "Final QC By"] == \
            head[head.index("Final QC Pass Date"):head.index("Final QC Pass Date") + 4]
        assert "Passed to Ready for Sale" not in head
        assert set(rows) == {tp}                       # fail and IQC -> Ready are not passes
        assert rows[tp]["Times Passed"] == 2 and rows[tp]["Passed To"] == "Ready to Sale"
        assert rows[tp]["Final QC Pass Date"] is not None
        old = (date.today() - timedelta(days=30)).isoformat()
        _t, _h, rows = export(tag=f"ITFQ{sfx}", work_from=old, work_to=old)
        assert rows == {}
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "(select id from devices where barcode like 'ITFQ{sfx}%')"
        await db.execute(text(f"delete from work_orders where device_id in {{ids}}"))
        await db.execute(text(f"delete from stage_movements where device_id in {{ids}}"))
        await db.execute(text("delete from devices where barcode like 'ITFQ{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
