"""Data Fixes (2026-10-28): WorkIDs still open on tags already at Ready to
Sale / Sold close as completed at the time the tag reached Ready to Sale."""
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


def test_rts_open_workids_close_at_ready_to_sale_time(app_client, make_user):  # noqa: F811
    bc = "ITRTS" + uuid.uuid4().hex[:8]
    wid = "ZRTS" + uuid.uuid4().hex[:8]
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime, timedelta
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, StageMovement
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.ready_to_sale)
        db.add(d); await db.flush()
        t0 = datetime(2026, 10, 1, 10, 0)
        db.add(WorkOrder(work_id="{wid}", device_id=d.id, barcode="{bc}", stage="l1", status="pending",
                         created_at=t0, assigned_at=t0, assigned_username="x", assigned_name="X"))
        db.add(StageMovement(device_id=d.id, from_stage=DeviceStage.final_qc_pass_hold,
                             to_stage=DeviceStage.ready_to_sale, moved_at=datetime(2026, 10, 5, 12, 30), moved_by="t"))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        page = app_client.get("/admin/system/data-fixes").text
        assert "WorkIDs still open on tags already at Ready to Sale" in page and wid in page
        r = app_client.post("/admin/system/rts-wo-close", data={"csrf_token": csrf},
                            headers={"X-CSRF-Token": csrf})
        assert r.json()["ok"] and r.json()["closed"] >= 1
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select status || '|' || to_char(completed_at, 'YYYY-MM-DD HH24:MI') from work_orders where work_id = :w"), {{"w": "{wid}"}})).scalar())
asyncio.run(main())
""")
        assert out == "completed|2026-10-05 12:30"
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from work_orders where work_id = :w"), {{"w": "{wid}"}})
        await db.execute(text("delete from stage_movements where device_id in (select id from devices where barcode = :b)"), {{"b": "{bc}"}})
        await db.execute(text("delete from devices where barcode = :b"), {{"b": "{bc}"}})
        await db.commit()
asyncio.run(main())
""")
