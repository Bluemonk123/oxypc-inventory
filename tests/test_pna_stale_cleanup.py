"""Data Fixes → Old PNA marks (2026-11-22, Pankaj): active PNA on a tag past
repair, or marked before the tag's last sale, is listed and cleared (not
deleted); a PNA marked in the current repair cycle is kept."""
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


def test_old_pna_marks_listed_and_cleared(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    past, before, fresh = f"PNOA{sfx}", f"PNOB{sfx}", f"PNOC{sfx}"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S, StageMovement
from models.pna_part import DevicePNAPart
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        for bc, st, marked, sold in (("{past}", S.ready_to_sale, datetime(2026, 9, 1), None),
                                     ("{before}", S.l1, datetime(2026, 9, 1), datetime(2026, 10, 1)),
                                     ("{fresh}", S.l1, datetime(2026, 10, 5), datetime(2026, 10, 1))):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=st)
            db.add(d); await db.flush()
            db.add(DevicePNAPart(device_id=d.id, barcode=bc, part_name="RAM", source="l1",
                                 marked_by="t", marked_at=marked, is_active=True))
            if sold:
                db.add(StageMovement(device_id=d.id, from_stage=S.ready_to_sale, to_stage=S.sold,
                                     moved_at=sold, moved_by="t"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get("/admin/system/data-fixes").text
        card = html.split('data-df="old-pna-marks"', 1)[1].split('data-df=', 1)[0]
        assert past in card and before in card and fresh not in card
        assert "Tag already at Ready for Sale" in card or "Tag already at Ready" in card
        assert "Marked before its last sale (01-10-2026)" in card
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/admin/system/pna-stale-clear", data={"csrf_token": csrf},
                            headers={"x-csrf-token": csrf})
        assert r.status_code == 200 and r.json()["cleared"] >= 2
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select barcode, is_active, cleared_by from device_pna_parts "
                                      "where barcode in ('{past}', '{before}', '{fresh}') order by barcode"))).all()
        print("|".join(f"{{b}}:{{a}}:{{c or ''}}" for b, a, c in rows))
asyncio.run(main())
""")
        got = {x.split(":")[0]: x.split(":", 1)[1] for x in out.split("|")}
        assert got[past].startswith("False:") and "(auto: cleanup)" in got[past]
        assert got[before].startswith("False:")
        assert got[fresh] == "True:"                       # current cycle: kept
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in ('{past}', '{before}', '{fresh}')"
        await db.execute(text(f"delete from device_pna_parts where device_id in ({{ids}})"))
        await db.execute(text(f"delete from stage_movements where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode in ('{past}', '{before}', '{fresh}')"))
        await db.commit()
asyncio.run(main())
""")
