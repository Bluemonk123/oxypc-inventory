"""External Tag return (2026-10-30): a tag already in the system moves to
Stock In, like a Customer return; a tag new to the system still starts at IQC."""
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import urlencode

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_external_return_moves_existing_tag_to_stock_in(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    old, new = f"ITEXS{sfx}O", f"ITEXS{sfx}N"
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        db.add(Device(id=uuid.uuid4(), barcode="{old}", lot_id=lot.id, brand="Dell", model="Latitude 7490",
                      current_stage=DeviceStage.sold))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        for tag in (old, new):
            r = app_client.post("/returns/new/external", content=urlencode({
                "csrf_token": csrf, "entity": "OxyPC Computers", "customer_name": "IT Ext", "reason": "Not working",
                "condition_on_return": "Minor damage", "tag_number": tag, "quantity": "1", "paid_repair": "0"}),
                headers={"X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False)
            assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{old}", "{new}"):
            print((await db.execute(text(f"select current_stage from devices where barcode = '{{bc}}'"))).scalar())
asyncio.run(main())
""").split()
        assert out == ["stock_in", "iqc"], out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{old}", "{new}"):
            ids = f"(select id from devices where barcode = '{{bc}}')"
            for t in ("returns", "stage_movements", "device_location_logs", "sales"):
                await db.execute(text(f"delete from {{t}} where device_id in {{ids}}"))
            await db.execute(text(f"delete from devices where barcode = '{{bc}}'"))
        await db.commit()
asyncio.run(main())
""")
