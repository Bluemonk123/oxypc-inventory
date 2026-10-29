"""Customise modal RAM / Storage (2026-10-29): the tables show Total RAM Size /
Total HDD Size first, so Customise must update those with the GB numbers —
setting only ram_gb / storage_gb left the shown value unchanged. The stick /
drive count is kept when it was already set."""
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


def test_customise_updates_shown_ram_and_storage(app_client, make_user):  # noqa: F811
    bc = "ITCUS" + uuid.uuid4().hex[:8].upper()
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                      ram_gb=8, total_ram_size="8 GB", ram_summary="8 GB", total_ram_count="2",
                      storage_gb=256, total_hdd_size="256 GB", hdd_summary="256 GB", total_hdd_count=""))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/iqc/bulk-apply-grade-type", data={
            "csrf_token": csrf, "barcodes": bc, "return_to": "/devices",
            "ram_gb": "16", "storage_gb": "512", "screen_size": "14.0 FHD"}, headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")
        row = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from utils.device_spec import ram_text, storage_text
async def main():
    async with AsyncSessionLocal() as db:
        d = (await db.execute(select(Device).where(Device.barcode == "{bc}"))).scalar_one()
        print("|".join(map(str, [d.screen_size, d.ram_gb, d.total_ram_size, d.ram_summary, d.total_ram_count,
                                 d.storage_gb, d.total_hdd_size, d.hdd_summary, d.total_hdd_count,
                                 ram_text(d), storage_text(d)])))
asyncio.run(main())
""")
        assert row == "14.0 FHD|16|16 GB|16 GB|2|512|512 GB|512 GB|1|16 GB|512 GB", row
        # Modal (2026-10-29): Screen Size before Device Price; Touch Type before Stock Type
        html = app_client.get("/devices").text
        m = html.split('id="customiseForm"', 1)[1]
        assert m.index('name="screen_size"') < m.index('name="device_price"')
        assert m.index('name="touch_type"') < m.index('name="stock_type"')
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")
