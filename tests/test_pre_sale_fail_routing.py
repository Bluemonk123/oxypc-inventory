"""Pre-Sale QC Fail (2026-11-23, Pankaj): Mark Complete → Fail asks for Final
QC's Fail category and routes the same way —
Hardware -> L1/L2 to its last L1/L2 engineer, else Production Manager
(trc_production); Software -> Stress Test; Cosmetic -> Cosmetic."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2500:]
    return r.stdout.strip()


def test_pre_sale_fail_routing(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:5].upper()
    hw, sw, cos = f"PFH{sfx}", f"PFS{sfx}", f"PFC{sfx}"
    tags = (hw, sw, cos)
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        for i, bc in enumerate({tags!r}):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=S.ready_to_sale, is_active=True)
            db.add(d); await db.flush()
            db.add(WorkOrder(work_id=f"PS{{i}}{sfx}", device_id=d.id, barcode=bc, stage="psiqc",
                             status="in_progress", assigned_username="zz_ps"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        csrf = app_client.cookies.get("csrf_token", "")
        h = {"x-csrf-token": csrf}

        def done(bc, cat):
            return app_client.post("/pre-sale-iqc/complete", headers=h, data={
                "csrf_token": csrf, "barcode": bc, "result": "fail", "category": cat, "reason": "found at Pre-Sale"}).json()
        bad = app_client.post("/pre-sale-iqc/complete", headers=h, data={
            "csrf_token": csrf, "barcode": hw, "result": "fail", "category": "Other"}).json()
        assert not bad["ok"] and "Fail category" in bad["error"]
        r = done(hw, "Hardware")
        assert r["ok"] and not r["kept"], r
        r = done(sw, "Software")
        assert r["ok"] and not r["kept"], r
        r = done(cos, "Cosmetic")
        assert r["ok"] and not r["kept"], r
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select barcode, current_stage from devices where barcode in {tags!r}"))).all()
        print(";".join(f"{{b}}={{s}}" for b, s in rows))
        print((await db.execute(text("select m.notes from stage_movements m join devices d on d.id = m.device_id "
                                     "where d.barcode = '{hw}' order by moved_at desc limit 1"))).scalar())
        print((await db.execute(text("select count(*) from work_orders where barcode in {tags!r} and stage = 'psiqc' "
                                     "and status = 'completed'"))).scalar())
asyncio.run(main())
""").splitlines()
        st = dict(x.split("=") for x in out[0].split(";"))
        assert st[hw] == "trc_production"          # Hardware, no L1/L2 engineer on record -> Production Manager
        assert st[sw] == "qc_check"                 # Software -> Stress Test
        assert st[cos].startswith("cosmetic"), st[cos]
        assert out[1].startswith("Pre-Sale QC Fail — Hardware") and "found at Pre-Sale" in out[1], out[1]
        assert out[2] == "3"
        page = app_client.get("/pre-sale-iqc").text
        assert "psqc_category" in page and "PSQC_FAIL_CATS" in page
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in {tags!r}"
        for t in ("work_orders", "stage_movements", "device_location_logs", "iqc_snapshots", "stock_transfers"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from notifications where message like any (array['%{hw}%', '%{sw}%', '%{cos}%'])"))
        await db.execute(text("delete from devices where barcode in {tags!r}"))
        await db.commit()
asyncio.run(main())
""")
