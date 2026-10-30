"""Edit Tag Details saves on Save itself (2026-10-30): a RAM / Hard Drive
value saved from the modal is not overwritten at Final QC pass by verified
part changes requested before that save. A part requested after the save
still applies."""
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


def test_modal_ram_and_hdd_survive_final_qc_pass(app_client, make_user):  # noqa: F811
    bc = "ITSPW" + uuid.uuid4().hex[:8].upper()
    _run(f"""
import asyncio, sys, uuid
from datetime import timedelta
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.part_request import PartRequest
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot.id, brand="Dell", model="Latitude 7490",
                   current_stage=DeviceStage.final_qc, ram_gb=8, storage_gb=256, grade="B")
        db.add(d); await db.flush()
        t = app_now() - timedelta(hours=2)
        for name, cap in (("RAM", "16GB"), ("Hard Drive", "512GB")):
            db.add(PartRequest(device_id=d.id, barcode="{bc}", part_name=name, part_capacity=cap, request_type="replace",
                               status="received", qty_requested=1, qty_handed_over=1, created_at=t, actioned_at=t))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post(f"/devices/{bc}/specs", data={"csrf_token": csrf, "ram": "32GB", "hard_drive": "1TB",
                                                          "grade": "A"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["ok"], r.text
        out = _run(f"""
import asyncio, sys
from datetime import timedelta
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.part_request import PartRequest
from services.parts_changed import apply_spec_changes_on_pass, _applied
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        d = (await db.execute(select(Device).where(Device.barcode == "{bc}"))).scalar_one()
        saved = (d.ram_gb, d.storage_gb, getattr(d.grade, "value", d.grade))
        applied = await apply_spec_changes_on_pass(db, d)        # Final QC pass
        after = (d.ram_gb, d.storage_gb)
        kept = len(await _applied(db, [d.id]))      # recorded as applied -> cells show the saved value
        # a part requested AFTER the modal save still applies
        db.add(PartRequest(device_id=d.id, barcode="{bc}", part_name="RAM", part_capacity="64GB", request_type="replace",
                           status="received", qty_requested=1, qty_handed_over=1,
                           created_at=app_now() + timedelta(minutes=1), actioned_at=app_now() + timedelta(minutes=1)))
        await db.flush()
        applied2 = await apply_spec_changes_on_pass(db, d)
        print(saved, len(applied), after, kept, len(applied2), d.ram_gb)
        await db.rollback()
asyncio.run(main())
""")
        assert out == "(32, 1024, 'A') 0 (32, 1024) 2 1 64", out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from part_requests where barcode = '{bc}'"))
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")
