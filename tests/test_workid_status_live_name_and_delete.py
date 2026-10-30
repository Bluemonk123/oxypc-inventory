"""WorkID Status (2026-10-30):
- Assigned Engineer shows the user's CURRENT Full Name, not the name stored
  on the WorkID when it was created (renaming a user updates past WorkIDs and
  the Team Productivity export);
- admin can delete a WorkID (trash icon); everyone else is refused; part
  requests under it are kept but unlinked; audit logged."""
import json
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


def test_live_name_and_admin_delete(app_client, make_user):  # noqa: F811
    bc = "ITWDL" + uuid.uuid4().hex[:8].upper()
    eng, _pw = make_user("l1_engineer")                     # full_name "IQC Test User"
    wid = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, select
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.user import User
from models.work_order import WorkOrder
from models.part_request import PartRequest
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        u = (await db.execute(select(User).where(User.username == "{eng}"))).scalar_one()
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.l1,
                   device_type="Laptop", brand="Dell", model="Latitude 7490")
        db.add(d); await db.flush()
        wid = "IT" + uuid.uuid4().hex[:10].upper()
        wo = WorkOrder(work_id=wid, device_id=d.id, barcode="{bc}", stage="l1", status="pending",
                       created_at=app_now(), assigned_at=app_now(), assigned_role="l1_engineer",
                       assigned_user_id=u.id, assigned_username=u.username, assigned_name="Old Name",
                       created_by="test")
        db.add(wo); await db.flush()
        db.add(PartRequest(work_order_id=wo.id, work_id=wid, device_id=d.id, barcode="{bc}", stage="l1",
                           part_name="RAM", request_type="new", status="requested", qty_requested=1,
                           requested_by="t"))
        await db.commit()
        print(wid)
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        html = app_client.get("/workid-status", params={"tag": bc, "work_from": "", "work_to": ""}).text
        rows = json.loads(html.split('id="workidRows">', 1)[1].split("</script>", 1)[0])
        assert [r["e"] for r in rows] == ["IQC Test User"], rows       # current name, not "Old Name"
        assert "wid-del-btn" in html and "var WID_ADMIN = true" in html

        # non-admin refused
        u2, pw2 = make_user("inventory_manager")
        _login(app_client, u2, pw2)
        csrf2 = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/workid-status/delete", data={"csrf_token": csrf2, "work_id": wid},
                            headers={"X-CSRF-Token": csrf2})
        assert r.status_code == 403
        assert "var WID_ADMIN = false" in app_client.get("/workid-status").text

        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/workid-status/delete", data={"csrf_token": csrf, "work_id": wid},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json() == {"ok": True, "work_id": wid, "unlinked": 1}, r.text
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        n = (await db.execute(text("select count(*) from work_orders where work_id = '{wid}'"))).scalar()
        pr = (await db.execute(text("select work_order_id, work_id from part_requests where barcode = '{bc}'"))).one()
        a = (await db.execute(text("select count(*) from audit_logs where action = 'WORKID_DELETED' "
                                   "and old_value like '%{wid}%'"))).scalar()
        print(n, pr[0], pr[1], a)
asyncio.run(main())
""")
        assert out == f"0 None {wid} 1", out
        r = app_client.post("/workid-status/delete", data={"csrf_token": csrf, "work_id": wid},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 404
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from part_requests where barcode = '{bc}'"))
        await db.execute(text("delete from work_orders where barcode = '{bc}'"))
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")
