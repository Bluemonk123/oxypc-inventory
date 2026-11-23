"""Move to L1/L2 (Stock table and Return table) always resets Pick This
(2026-11-22, Pankaj): every open L1/L2 and L3/L4 WorkID of the tag closes —
"l1", "l2", "L1L2-" and "L3L4-" alike — and one new unpicked WorkID opens."""
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


def test_return_move_to_l1l2_closes_every_old_workid(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    bc = f"ITMV{sfx}"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot.id, brand="Dell", model="Latitude 7490",
                   current_stage=DeviceStage.stock_in, return_status=True, l1l2_status="Repair Started")
        db.add(d); await db.flush()
        at = datetime(2026, 11, 1, 10, 0)
        for wid, code, st in (("A{sfx}", "l2", "in_progress"), ("L1L2-{sfx}", "l1", "pending"),
                              ("L3L4-{sfx}", "l3", "pending")):
            db.add(WorkOrder(work_id=wid, device_id=d.id, barcode="{bc}", stage=code, status=st,
                             created_at=at, assigned_at=at, assigned_username="old_eng", assigned_name="Old Eng"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        r = app_client.post("/stock/return-stock/move-l1l2", data={"barcodes": bc},
                            headers={"x-csrf-token": app_client.cookies.get("csrf_token", "")})
        assert r.status_code == 200 and r.json().get("moved") == 1, r.text
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        d = (await db.execute(select(Device).where(Device.barcode == "{bc}"))).scalars().one()
        rows = (await db.execute(select(WorkOrder).where(WorkOrder.device_id == d.id))).scalars().all()
        open_ = [w for w in rows if w.status not in ("completed", "reassigned")]
        print(d.current_stage.value, d.l1l2_status, len(rows), len(open_),
              open_[0].assigned_username if open_ else "-")
asyncio.run(main())
""")
        stage, status, total, n_open, who = out.split()
        assert stage == "l1" and status == "New"
        assert total == "4" and n_open == "1" and who == "None", out    # one fresh, unpicked WorkID
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode = '{bc}'"
        for t in ("work_orders", "stage_movements"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")
