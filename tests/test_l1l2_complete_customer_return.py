"""L1/L2 Mark Complete for a customer return (2026-10-30): a tag with
Return Status = Yes moves straight to Sold — stage only, no new sale, no
Stress Test WorkID. A normal tag still goes to Stress Test."""
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


def test_customer_return_mark_complete_goes_to_sold(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    ret_bc, norm_bc = f"ITCRS{sfx}R", f"ITCRS{sfx}N"
    ids = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        out = []
        for bc, rs in (("{ret_bc}", True), ("{norm_bc}", False)):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot.id, brand="Dell", model="Latitude 7490",
                       current_stage=DeviceStage.l1, l1l2_status="Repair Started", return_status=rs)
            db.add(d); out.append(str(d.id))
        await db.commit()
        print(" ".join(out))
asyncio.run(main())
""").split()
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        page = app_client.get("/repair/l1").text
        assert f'data-customer-return="{ret_bc}"' in page or ret_bc not in page   # row may be paged out
        for did in ids:
            r = app_client.post("/repair/l1l2-complete-to-stress", data={"csrf_token": csrf, "device_id": did},
                                headers={"X-CSRF-Token": csrf}, follow_redirects=False)
            assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{ret_bc}", "{norm_bc}"):
            st = (await db.execute(text(f"select current_stage from devices where barcode = '{{bc}}'"))).scalar()
            sales = (await db.execute(text(f"select count(*) from sales s join devices d on d.id = s.device_id where d.barcode = '{{bc}}'"))).scalar()
            strs = (await db.execute(text(f"select count(*) from work_orders where barcode = '{{bc}}' and work_id like 'STRS-%'"))).scalar()
            print(bc[-1], st, sales, strs)
asyncio.run(main())
""").splitlines()
        assert out == ["R sold 0 0", "N qc_check 0 1"], out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{ret_bc}", "{norm_bc}"):
            ids = f"(select id from devices where barcode = '{{bc}}')"
            await db.execute(text(f"delete from work_orders where barcode = '{{bc}}'"))
            await db.execute(text(f"delete from stage_movements where device_id in {{ids}}"))
            await db.execute(text(f"delete from devices where barcode = '{{bc}}'"))
        await db.commit()
asyncio.run(main())
""")
