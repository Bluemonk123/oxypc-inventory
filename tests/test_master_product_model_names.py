"""Master Data -> Product Model Names (2026-11-06, Pankaj): a full-width
dropdown section (values in 1-4 columns) with a Reload button that adds every
model name found on the tags — one name per model, nothing deleted, names
already in the list kept as they are."""
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


def test_reload_adds_one_name_per_model(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, insert
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.master import MasterData
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        rows = [(None, "Pqm Note {sfx} G2")] * 3 + [(None, "PQM NOTE {sfx}G2"), (None, "Pqm Tab {sfx}"),
                ("ZQBRAND{sfx}", "Pqm Note {sfx} G2"), ("Zqbrand{sfx}", "PQM NOTE {sfx} G2"), ("Zqbrand{sfx}", "Pqm Note {sfx} G2")]
        for i, (b, m) in enumerate(rows):
            await db.execute(insert(Device).values(id=uuid.uuid4(), barcode=f"ITPM{sfx}{{i}}", lot_id=lot, brand=b,
                                                   current_stage=DeviceStage.stock_in, model=m))
        db.add(MasterData(category="product_model_name", value="PQM-TAB {sfx}", is_active=False))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        html = app_client.get("/admin/master").text
        assert 'id="section-product_models"' in html and "/admin/master/product-model-names/reload" in html
        assert "row-cols-1 row-cols-sm-2 row-cols-lg-3 row-cols-xl-4" in html
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        for _ in range(2):                       # a second Reload adds nothing
            r = app_client.post("/admin/master/product-model-names/reload", data={"csrf_token": csrf},
                                headers={"X-CSRF-Token": csrf}, follow_redirects=False)
            assert r.status_code == 302 and "reloaded" in r.headers["location"]
        got = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print("|".join(sorted((await db.execute(text(
            "select coalesce(parent, '-') || '/' || value from master_data "
            "where category='product_model_name' and value ilike '%{sfx}%'"))).scalars().all())))
asyncio.run(main())
""").splitlines()[-1]
        # one entry per Make + model (model in its canonical spelling, Make in the
        # tags' most-used spelling); the Tab model was already in the list in
        # another spelling (and disabled) so it isn't added again
        assert got == f"-/PQM-TAB {sfx}|-/Pqm Note {sfx} G2|Zqbrand{sfx}/Pqm Note {sfx} G2", got
        # linked lookup: only that Make's models (any capitals)
        r = app_client.get(f"/admin/master/api/product_model_name", params={"parent": f"ZQBRAND{sfx}"})
        assert r.json()["values"] == [f"Pqm Note {sfx} G2"], r.text
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode like 'ITPM{sfx}%'"))
        await db.execute(text("delete from master_data where category='product_model_name' and value ilike '%{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
