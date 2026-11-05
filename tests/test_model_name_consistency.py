"""Product Model Name consistency (2026-11-05, Pankaj): one spelling per model.
Same model = same letters and digits ignoring case, spaces and punctuation.
Data Fix tidies existing tags; the Device listener keeps new saves consistent."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from utils.model_name import changes, choose, fix_groups, model_key, overrides_from, tidy

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_rules():
    assert tidy("\tEliteBook  x360 1030 G7 ") == "EliteBook x360 1030 G7"
    assert model_key("EliteBook x360 1030G7") == model_key("Elitebook X360 1030 G7")
    assert choose({"EliteBook x360 1030 G7": 3473, "Elitebook X360 1030 G7": 65,
                   "EliteBook x360 1030G7": 75}) == "EliteBook x360 1030 G7"
    assert choose({"ZBOOK STUDIO G7": 575, "Zbook Studio G7": 455}) == "Zbook Studio G7"   # not shouted
    assert choose({"OPTIPLEX 3070": 12, "Optiplex3070": 4}) == "Optiplex 3070"            # case + spacing
    assert choose({"E2030": 13, "E20 30": 1}) == "E2030"          # a space inside a number doesn't count
    assert choose({"840g6": 10, "840 G6": 300}) == "840 G6"
    assert choose({"S24E310HL": 400}) == "S24E310HL"              # model codes aren't "shouting"
    ch = changes([("ThinkPad T490", 100), ("Thinkpad T490", 5), ("ThinkpadT490", 1), ("Latitude 5420", 9)])
    assert ch == [("Thinkpad T490", "ThinkPad T490", 5), ("ThinkpadT490", "ThinkPad T490", 1)]
    # a typed name wins over the automatic one
    rows = [("ThinkPad T490", 100), ("Thinkpad T490", 5)]
    typed = overrides_from(["Thinkpad T-490"])
    assert changes(rows, typed) == [("ThinkPad T490", "Thinkpad T-490", 100), ("Thinkpad T490", "Thinkpad T-490", 5)]
    g = fix_groups(rows, typed)[0]
    assert g["auto"] == "ThinkPad T490" and g["final"] == "Thinkpad T-490" and g["typed"] and g["to_change"] == 105


def test_save_snaps_and_data_fix_lists(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    canon, odd = f"Zqx Book {sfx} G7", f"ZQX BOOK {sfx}G7"
    out = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, insert
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from utils.model_name import clear_cache
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        # existing data: written with a core INSERT (no listener), one odd spelling
        for i, m in enumerate(["{canon}"] * 3 + ["{odd}"]):
            await db.execute(insert(Device).values(id=uuid.uuid4(), barcode=f"ITMN{sfx}{{i}}", lot_id=lot,
                                                   current_stage=DeviceStage.stock_in, model=m))
        await db.commit()
        clear_cache()
        # a new tag typed another way -> saved as the model's spelling
        d = Device(id=uuid.uuid4(), barcode="ITMN{sfx}N", lot_id=lot, current_stage=DeviceStage.stock_in,
                   model="  zqx book {sfx.lower()} g7 ")
        db.add(d); await db.commit()
        # editing a tag's model to another spelling of it -> snapped too
        d.model = "Zqx-Book-{sfx}-G7"; await db.commit()
        e = Device(id=uuid.uuid4(), barcode="ITMN{sfx}X", lot_id=lot, current_stage=DeviceStage.stock_in,
                   model="  Brand New\\tModel {sfx} ")
        db.add(e); await db.commit()
        print(d.model + "|" + e.model)
asyncio.run(main())
""")
    try:
        got_d, got_e = out.splitlines()[-1].split("|")
        assert got_d == canon
        assert got_e == f"Brand New Model {sfx}"        # a new model: kept, spaces tidied
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/admin/system/data-fixes").text
        assert "Product Model Name" in html and "/admin/system/model-name-fix" in html
        assert odd in html                                # the odd spelling is listed to fix
        key = model_key(canon)
        assert f'name="mn_{key}"' in html and f'value="{canon}"' in html   # Save-as box, filled in
        # Apply with a typed name (capitals / spaces changed) + one refused name
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        typed = f"ZQX-Book {sfx} G7"
        r = app_client.post("/admin/system/model-name-fix", follow_redirects=False,
                            data={"csrf_token": csrf, "back": "1", f"mn_{key}": typed}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 303
        got = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ms = (await db.execute(text("select distinct model from devices where barcode like 'ITMN{sfx}%' and barcode <> 'ITMN{sfx}X'"))).scalars().all()
        ov = (await db.execute(text("select count(*) from master_data where category='model_spelling' and value=:v"), {{"v": "{typed}"}})).scalar()
        print("|".join(ms) + "#" + str(ov))
asyncio.run(main())
""").splitlines()[-1]
        assert got == f"{typed}#1", got
        # remembered: a new save of another spelling follows the typed name
        later = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="ITMN{sfx}L", lot_id=lot, current_stage=DeviceStage.stock_in,
                   model="ZQX BOOK {sfx} G7")
        db.add(d); await db.commit(); print(d.model)
asyncio.run(main())
""").splitlines()[-1]
        assert later == typed
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode like 'ITMN{sfx}%'"))
        await db.execute(text("delete from master_data where category='model_spelling' and value ilike '%{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
