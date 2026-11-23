"""Production Manager → Move to Stage → Pre-Sale QC (2026-11-22, Pankaj): the
tag goes to Ready for Sale (when it isn't there) and is queued for Pre-Sale QC
(one pending, unassigned PSQC- WorkID); other open WorkIDs close as
reassigned. The option sits just before TRC Production."""
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


def test_move_to_pre_sale_qc(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    fqc, rts = f"PSMA{sfx}", f"PSMB{sfx}"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        a = Device(id=uuid.uuid4(), barcode="{fqc}", lot_id=lot, current_stage=S.final_qc, is_active=True)
        b = Device(id=uuid.uuid4(), barcode="{rts}", lot_id=lot, current_stage=S.ready_to_sale, is_active=True)
        db.add_all([a, b]); await db.flush()
        db.add(WorkOrder(work_id="FQ{sfx}", device_id=a.id, barcode="{fqc}", stage="fqc", status="pending",
                         created_at=datetime(2026, 11, 20, 10), assigned_username="old_eng"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get("/trc-production").text
        sel = html.split('id="ceModal_stage"', 1)[1].split("</select>", 1)[0]
        assert sel.index('value="pre_sale_iqc"') < sel.index('value="trc_production"')
        assert ">Pre-Sale QC<" in sel
        csrf = app_client.cookies.get("csrf_token", "")
        h = {"x-csrf-token": csrf}
        r = app_client.post("/devices/change-engineer", data={"barcode": fqc, "target_stage": "pre_sale_iqc",
                                                              "csrf_token": csrf}, headers=h)
        assert r.status_code == 200 and r.json()["ok"] and r.json()["work_id"].startswith("PSQC-"), r.text
        again = app_client.post("/devices/change-engineer", data={"barcode": fqc, "target_stage": "pre_sale_iqc",
                                                                  "csrf_token": csrf}, headers=h)
        assert again.status_code == 400                       # already queued
        r2 = app_client.post("/devices/change-engineer", data={"barcode": rts, "target_stage": "pre_sale_iqc",
                                                               "csrf_token": csrf}, headers=h)
        assert r2.status_code == 200 and r2.json()["ok"]
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        st = dict((await db.execute(text("select barcode, current_stage from devices where barcode in ('{fqc}', '{rts}')"))).all())
        wos = (await db.execute(text("select barcode, stage, status, coalesce(assigned_username, '') from work_orders "
                                     "where barcode in ('{fqc}', '{rts}') order by barcode, stage"))).all()
        print(st["{fqc}"], st["{rts}"], ";".join("|".join(map(str, w)) for w in wos))
asyncio.run(main())
""")
        st_a, st_b, wos = out.split(" ", 2)
        assert st_a == "ready_to_sale" and st_b == "ready_to_sale"
        rows = wos.split(";")
        assert f"{fqc}|fqc|reassigned|old_eng" in rows
        assert f"{fqc}|psiqc|pending|" in rows and f"{rts}|psiqc|pending|" in rows
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in ('{fqc}', '{rts}')"
        for t in ("work_orders", "stage_movements"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from device_location_logs where device_id in (select id from devices where barcode in ('{fqc}', '{rts}'))"))
        await db.execute(text("delete from devices where barcode in ('{fqc}', '{rts}')"))
        await db.commit()
asyncio.run(main())
""")
