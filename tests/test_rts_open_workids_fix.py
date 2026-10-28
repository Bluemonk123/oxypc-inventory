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


def test_asgn_left_open_and_wrongly_closed_ones_reopen(app_client, make_user):  # noqa: F811
    """"asgn" (Inventory Request Assign To Employee) WorkIDs are never closed by
    the Ready to Sale fix; ones the first version closed are reopened."""
    bc = "ITRTA" + uuid.uuid4().hex[:8]
    w_open, w_closed = "ZRA" + uuid.uuid4().hex[:8], "ZRB" + uuid.uuid4().hex[:8]
    _run(f"""
import asyncio, sys, uuid, json
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.work_order import WorkOrder
from models.engines import AuditLog
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.ready_to_sale)
        db.add(d); await db.flush()
        t = datetime(2026, 10, 28, 15, 31)
        db.add(WorkOrder(work_id="{w_open}", device_id=d.id, barcode="{bc}", stage="asgn", status="pending",
                         created_at=t, assigned_at=t, assigned_username="x", assigned_name="X"))
        closed = WorkOrder(id=uuid.uuid4(), work_id="{w_closed}", device_id=d.id, barcode="{bc}", stage="asgn",
                           status="completed", completed_at=datetime(2026, 10, 28, 15, 40),
                           created_at=t, assigned_at=t, assigned_username="x", assigned_name="X")
        db.add(closed); await db.flush()
        db.add(AuditLog(action="WORKID_RTS_CLOSE", table_name="work_orders", record_id="1 WorkIDs",
                        new_value=json.dumps({{"count": 1, "work_orders": {{str(closed.id): {{
                            "work_id": "{w_closed}", "tag": "{bc}", "completed_at": "2026-10-28T15:40:00"}}}}}})))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        page = app_client.get("/admin/system/data-fixes").text
        assert w_closed in page and "Reopen Inventory Request assignments" in page
        app_client.post("/admin/system/rts-wo-close", data={"csrf_token": csrf}, headers={"X-CSRF-Token": csrf})
        r = app_client.post("/admin/system/rts-asgn-reopen", data={"csrf_token": csrf}, headers={"X-CSRF-Token": csrf})
        assert r.json()["ok"] and r.json()["reopened"] >= 1
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select work_id, status, completed_at is null from work_orders where work_id in (:a, :b) order by work_id"), {{"a": "{w_open}", "b": "{w_closed}"}})).all()
        print(";".join(f"{{w}}|{{s}}|{{n}}" for w, s, n in rows))
asyncio.run(main())
""")
        assert out == f"{w_open}|pending|True;{w_closed}|pending|True"
        assert app_client.get("/admin/system/error-log").status_code == 200
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from work_orders where work_id in (:a, :b)"), {{"a": "{w_open}", "b": "{w_closed}"}})
        await db.execute(text("delete from audit_logs where action = 'WORKID_RTS_CLOSE' and new_value like :p"), {{"p": "%{w_closed}%"}})
        await db.execute(text("delete from devices where barcode = :b"), {{"b": "{bc}"}})
        await db.commit()
asyncio.run(main())
""")
