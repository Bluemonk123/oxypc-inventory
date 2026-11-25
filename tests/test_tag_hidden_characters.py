"""Tag Numbers with hidden characters (2026-11-24): a tag saved as
"3579040225L0249<TAB>" could not be opened (its link drops the TAB).
- the page finds it with such characters ignored (detail + edit);
- every new save cleans the Tag Number (ORM validator);
- Data Fixes cleans existing ones, incl. the copy on WorkIDs."""
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


def test_tag_hidden_characters(app_client, make_user):  # noqa: F811
    clean = f"HTC{uuid.uuid4().hex[:8].upper()}"
    clean2 = clean + "N"
    out = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        did = uuid.uuid4()
        # raw insert, as an old upload did: TAB at the end
        await db.execute(text("insert into devices (id, barcode, lot_id, current_stage, is_active, is_trashed, created_at) "
                              "values (:i, :b, :l, 'l1', true, false, now())"), {{"i": did, "b": "{clean}\\t", "l": lot}})
        await db.execute(text("insert into work_orders (id, work_id, device_id, barcode, stage, status, created_at, assigned_at) "
                              "values (gen_random_uuid(), :w, :i, :b, 'l1', 'pending', now(), now())"),
                         {{"w": "HT" + "{clean}"[-8:], "i": did, "b": "{clean}\\t"}})
        # a new save through the app is cleaned
        d2 = Device(id=uuid.uuid4(), barcode=" {clean2}\\u200b\\t", lot_id=lot, current_stage=S.l1, is_active=True)
        db.add(d2); await db.commit()
        print(repr((await db.execute(text("select barcode from devices where id = :i"), {{"i": d2.id}})).scalar()))
asyncio.run(main())
""")
    try:
        assert out == repr(clean2)
        u, p = make_user("admin")
        _login(app_client, u, p)
        assert app_client.get(f"/devices/{clean}").status_code == 200          # opens despite the TAB
        assert app_client.get(f"/devices/{clean}/edit").status_code == 200
        df = app_client.get("/admin/system/data-fixes").text
        card = df.split('data-df="tag-clean"', 1)[1].split('data-df=', 1)[0]
        assert f"{clean}\\\\t" in card or f"{clean}\\t" in card, card[:600]
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/admin/system/tag-clean", data={"csrf_token": csrf}, headers={"x-csrf-token": csrf}).json()
        assert r["ok"] and r["cleaned"] >= 1
        got = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        a = (await db.execute(text("select count(*) from devices where barcode = '{clean}'"))).scalar()
        b = (await db.execute(text("select barcode from work_orders where barcode like '{clean}%'"))).scalar()
        print(a, repr(b))
asyncio.run(main())
""")
        assert got == f"1 {clean!r}", got
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from utils.fk_purge import purge_references
async def main():
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(text("select id from devices where barcode like '%{clean}%'"))).scalars().all()
        if ids:
            await purge_references(db, "devices", ids)
            await db.execute(text("delete from devices where id = any(:i)"), {{"i": ids}})
        await db.commit()
asyncio.run(main())
""")
