"""Data Fixes → Customer returns in L1/L2 (2026-11-27, Pankaj): move them to
RMA Devices (RMA engineers work only there). Replaced / Credit Note tags stay.
Return Stock leaves Credit Note returns out (tracked on the Credit Note page)."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_l1_returns_move_to_rma_and_credit_notes_leave_return_stock(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    cr, cn, ow = f"ITL1R{sfx}C", f"ITL1R{sfx}N", f"ITL1R{sfx}O"
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Return
from models.work_order import WorkOrder
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).where(Lot.is_trashed.isnot(True)).limit(1))).scalars().first()
        for bc, trs, ret, stage in (("{cr}", "Return for Repair", True, DeviceStage.l1),
                                    ("{cn}", "Return for Credit Note", True, DeviceStage.sold),
                                    ("{ow}", None, False, DeviceStage.l1)):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot.id, current_stage=stage, return_status=ret,
                       tag_return_status=trs, l1l2_status="Repair Started" if bc == "{cr}" else None, is_active=True)
            db.add(d); await db.flush()
            if ret:
                db.add(Return(device_id=d.id, processed_by="t", return_date=app_now()))
            if stage == DeviceStage.l1:
                db.add(WorkOrder(work_id=("W" + bc)[-12:], device_id=d.id, barcode=bc, stage="l1", status="in_progress",
                                 created_at=app_now(), assigned_at=app_now(), assigned_username="eng1", assigned_name="Eng One"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        csrf = app_client.cookies.get("csrf_token") or "x"
        page = app_client.get("/admin/system/data-fixes").text
        card = page.split('data-df="l1-returns-to-rma"', 1)[1].split('data-df=', 1)[0]
        assert cr in card and cn not in card and ow not in card and "Eng One" in card
        stock = app_client.get("/stock").text
        rs = stock[stock.index('id="returnStockTable"'):]
        assert cn not in rs                       # Credit Note return: not Return Stock
        r = app_client.post("/admin/system/l1-returns-to-rma", data={"csrf_token": csrf},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["moved"] >= 1, r.text
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{cr}", "{ow}"):
            print(bc, (await db.execute(text("select current_stage||'|'||coalesce(l1l2_status,'') from devices where barcode=:b"), {{"b": bc}})).scalar(),
                  sorted((await db.execute(text("select stage||':'||status from work_orders where barcode=:b"), {{"b": bc}})).scalars().all()))
asyncio.run(main())
""").splitlines()
        assert out[0] == f"{cr} rma|Repair Started ['l1:reassigned', 'rma:pending']", out
        assert out[1].startswith(f"{ow} l1|") and out[1].endswith("['l1:in_progress']"), out
        assert cr in app_client.get("/repair/rma").text
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode like 'ITL1R{sfx}%'"
        fk = "select c.conrelid::regclass::text, a.attname from pg_constraint c join pg_attribute a " \\
             "on a.attrelid = c.conrelid and a.attnum = c.conkey[1] where c.contype = 'f' and c.confrelid = 'devices'::regclass"
        for t, col in (await db.execute(text(fk))).all():
            if t != "devices":
                await db.execute(text(f"delete from {{t}} where {{col}} in ({{ids}})"))
        await db.execute(text(f"delete from devices where id in ({{ids}})"))
        await db.commit()
asyncio.run(main())
""")
