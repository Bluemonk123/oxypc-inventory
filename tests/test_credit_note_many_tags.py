"""Credit Note for many tags (2026-10-30): six 15-character Tag Numbers made
the audit record_id ("tag,tag,...", 95 chars) overflow audit_logs.record_id
VARCHAR(50) and the submit returned 500 on production. The audit engine now
keeps the first 50 characters there and the full value in notes."""
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


def test_credit_note_six_tags(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tags = [f"ITCN{sfx}L{i:04d}" for i in range(6)]          # 15 chars each, like 4484210526L2781
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for t in {tags!r}:
            db.add(Device(id=uuid.uuid4(), barcode=t, lot_id=lot, current_stage=DeviceStage.sold,
                          device_type="Laptop", brand="Dell", model="Latitude 7490"))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        data = [("csrf_token", csrf), ("debit_note_number", f"DN-{sfx}"), ("debit_note_amount", "1000")]
        data += [("barcode", t) for t in tags]
        from urllib.parse import urlencode
        r = app_client.post("/returns/new/credit-note", content=urlencode(data),
                            headers={"X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded"},
                            follow_redirects=False)
        assert r.status_code == 302 and "success=6" in r.headers["location"], (r.status_code, r.headers.get("location"))
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        n = (await db.execute(text("select count(*) from returns r join devices d on d.id = r.device_id "
                                   "where d.barcode like 'ITCN{sfx}%' and r.cn_stage is not null"))).scalar()
        rid, notes = (await db.execute(text("select record_id, notes from audit_logs where action = "
            "'CREDIT_NOTE_DEBIT_NOTE_SET' and new_value like '%DN-{sfx}%'"))).one()
        print(n, len(rid), "{tags[-1]}" in (notes or ""))
asyncio.run(main())
""")
        assert out == "6 50 True", out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "(select id from devices where barcode like 'ITCN{sfx}%')"
        await db.execute(text(f"delete from returns where device_id in {{ids}}"))
        await db.execute(text("delete from devices where barcode like 'ITCN{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
