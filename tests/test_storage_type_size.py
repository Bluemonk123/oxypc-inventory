"""A size typed into Storage Type (2026-10-29): summaries showed it twice
("256 GB 256 GB"). storage_display() builds one storage text; the Data Fixes
card moves such sizes into the storage fields (no GB / TB correction) and
leaves a tag alone when a different size is already there."""
import pathlib
import subprocess
import sys
import uuid
from types import SimpleNamespace as N

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def _d(**kw):
    base = dict(total_hdd_size=None, storage_gb=None, hdd_capacity_gb=None, hdd_summary=None, storage_type=None)
    base.update(kw)
    return N(**base)


def test_storage_display():
    from utils.device_spec import storage_display
    assert storage_display(_d(storage_gb=256, storage_type="SSD")) == "256 GB SSD"
    assert storage_display(_d(storage_gb=256, total_hdd_size="256 GB", storage_type="256 GB")) == "256 GB"
    assert storage_display(_d(total_hdd_size="0", storage_type="256 GB")) == "256 GB"
    assert storage_display(_d(storage_gb=256, storage_type="512 GB")) == "256 GB"
    assert storage_display(_d(storage_type="0")) == ""
    assert storage_display(_d(storage_gb=1, total_hdd_size="1 TB")) == "1 TB"


def test_storage_type_fix_and_model_summary(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    move, same, zero, diff = (f"ITST{sfx}{c}" for c in "MSZD")
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for bc, kw in [("{move}", dict(total_hdd_size="0", storage_type="256 GB", total_hdd_count="0")),
                       ("{same}", dict(storage_gb=256, total_hdd_size="256 GB", storage_type="256 GB")),
                       ("{zero}", dict(storage_type="0")),
                       ("{diff}", dict(storage_gb=512, total_hdd_size="512 GB", storage_type="1 TB"))]:
            db.add(Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                          model="ITST{sfx}", brand="Dell", **kw))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        # Model Summary: one text, never "256 GB 256 GB"
        html = app_client.get("/sales/ready").text
        assert "256 GB 256 GB" not in html

        page = app_client.get("/admin/system/data-fixes").text
        assert "Storage size typed into Storage Type" in page and diff in page   # listed, left alone
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/admin/system/storage-type-fix", data={"csrf_token": csrf},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["ok"]
        rows = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for r in (await db.execute(text("select barcode, storage_gb, total_hdd_size, hdd_summary, total_hdd_count, "
                                        "coalesce(storage_type, '-') from devices where barcode like 'ITST{sfx}%' "
                                        "order by barcode"))).all():
            print("|".join(str(x) for x in r))
asyncio.run(main())
""").splitlines()
        got = {r.split("|")[0]: r.split("|")[1:] for r in rows}
        assert got[move] == ["256", "256 GB", "256 GB", "1", "-"]
        assert got[same] == ["256", "256 GB", "None", "None", "-"]
        assert got[zero][-1] == "-"
        assert got[diff] == ["512", "512 GB", "None", "None", "1 TB"]     # different size: untouched
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode like 'ITST{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
