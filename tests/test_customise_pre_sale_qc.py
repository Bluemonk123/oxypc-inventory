"""All Inventories → Customise → Move to Stage (2026-11-22, Pankaj):
"Pre-Sale QC" sits just before Sold and works like every other way into
Pre-Sale QC — the tag goes to Ready for Sale and is queued (one pending
PSQC- WorkID), from any stage. Final QC Pass / Fail Hold are no longer in
this list."""
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


def test_customise_pre_sale_qc(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    a, b = f"CPSA{sfx}", f"CPSB{sfx}"
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{a}", lot_id=lot, current_stage=S.painting, is_active=True))
        db.add(Device(id=uuid.uuid4(), barcode="{b}", lot_id=lot, current_stage=S.ready_to_sale, is_active=True))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get("/devices").text
        sel = html.split('name="to_stage"', 1)[1].split("</select>", 1)[0]
        assert 'value="final_qc_pass_hold"' not in sel and 'value="final_qc_fail_hold"' not in sel
        assert sel.index('value="pre_sale_iqc"') < sel.index('value="sold"')
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/iqc/bulk-apply-grade-type", data={
            "csrf_token": csrf, "barcodes": [a, b], "return_to": "/devices", "to_stage": "pre_sale_iqc"},
            headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        loc = r.headers.get("location", "")
        assert r.status_code == 302 and "error" not in loc and "2+queued+for+Pre-Sale+QC" in loc, loc
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "select d.barcode, d.current_stage, (select count(*) from work_orders w where w.device_id = d.id "
            "and w.stage = 'psiqc' and w.status = 'pending') from devices d where d.barcode in ('{a}', '{b}') "
            "order by d.barcode"))).all()
        print(";".join("|".join(map(str, x)) for x in rows))
asyncio.run(main())
""")
        assert out == f"{a}|ready_to_sale|1;{b}|ready_to_sale|1", out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in ('{a}', '{b}')"
        for t in ("work_orders", "stage_movements", "device_location_logs"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode in ('{a}', '{b}')"))
        await db.commit()
asyncio.run(main())
""")
