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
        assert f'name="mn_{key}|' in html and f'value="{canon}"' in html   # Save-as box, filled in
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


def test_rename_rule():
    """2026-11-06: a name with different letters / numbers is a rename,
    remembered as "old => new" so old spellings saved later follow it."""
    from utils.model_name import parse_override, rename_value
    v = rename_value("Prodesk 400 G4 SFF", "Prodesk 400 G4")
    assert v == "Prodesk 400 G4 SFF => Prodesk 400 G4"
    assert parse_override(v) == ("prodesk400g4sff", "Prodesk 400 G4")
    ov = overrides_from([v, "Prodesk 400 G4"])
    assert ov == {"prodesk400g4sff": "Prodesk 400 G4", "prodesk400g4": "Prodesk 400 G4"}
    assert changes([("PRODESK 400 G4 SFF", 106), ("Prodesk 400 G4 Sff", 20)], ov) == [
        ("PRODESK 400 G4 SFF", "Prodesk 400 G4", 106), ("Prodesk 400 G4 Sff", "Prodesk 400 G4", 20)]


def test_apply_renames_a_row(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    old_a, old_b, new = f"QZW Desk {sfx} SFF", f"Qzw Desk {sfx} Sff", f"Qzw Desk {sfx}"
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, insert
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for i, m in enumerate(["{old_a}"] * 2 + ["{old_b}"]):
            await db.execute(insert(Device).values(id=uuid.uuid4(), barcode=f"ITMR{sfx}{{i}}", lot_id=lot,
                                                   current_stage=DeviceStage.stock_in, model=m))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        key = model_key(old_a)
        assert f'name="mn_{key}|' in app_client.get("/admin/system/data-fixes").text
        r = app_client.post("/admin/system/model-name-fix",
                            data={"csrf_token": csrf, "back": "1", "ajax": "1", f"mn_{key}": new},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["ok"] and "Renamed" in r.json()["message"], r.text
        got = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ms = (await db.execute(text("select distinct model from devices where barcode like 'ITMR{sfx}%'"))).scalars().all()
        ov = sorted((await db.execute(text("select value from master_data where category='model_spelling' and value ilike '%{sfx}%'"))).scalars().all())
        print("|".join(ms) + "#" + "~".join(ov))
asyncio.run(main())
""").splitlines()[-1]
        models, ov = got.split("#")
        assert models == new, got                                   # every spelling renamed
        ov = set(ov.split("~"))
        assert new in ov and len(ov) == 2, got                      # the new name + one "old => new"
        assert ({f"{old_a} => {new}", f"{old_b} => {new}"} & ov), got
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode like 'ITMR{sfx}%'"))
        await db.execute(text("delete from master_data where category='model_spelling' and value ilike '%{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")


def test_rows_by_make_and_export(app_client, make_user):  # noqa: F811
    """2026-11-06: one row per Model + Make (a model under two Makes shows as
    two rows, even when the spelling is fine), and each spelling downloads
    its tags with hardware, stage, PNA, Return and Scrap."""
    from utils.model_name import fix_groups_by_make
    g = fix_groups_by_make([("Dell", "Latitude 7490", 50), ("HP", "Latitude 7490", 2), ("Dell", "Latitude 5490", 9)])
    assert [(x["make_names"], x["to_change"], x["makes"]) for x in g] == [(["Dell"], 0, 2), (["HP"], 0, 2)]
    # capitals / spaces in the Make don't make a second Make
    g = fix_groups_by_make([("Dell", "Latitude 7490", 50), ("DELL", "Latitude 7490", 2)])
    assert g == []
    g = fix_groups_by_make([("Dell", "LATITUDE 7490", 3), ("Dell", "Latitude 7490", 50)])
    assert len(g) == 1 and g[0]["final"] == "Latitude 7490" and g[0]["to_change"] == 3

    sfx = uuid.uuid4().hex[:6].upper()
    model = f"Qxv Pad {sfx}"
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, insert
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for i, (mk, st) in enumerate([("Dell", "stock_in"), ("Dell", "scrapped"), ("HP", "stock_in")]):
            await db.execute(insert(Device).values(id=uuid.uuid4(), barcode=f"ITMK{sfx}{{i}}", lot_id=lot, brand=mk,
                                                   current_stage=DeviceStage(st), model="{model}", cpu="i5", ram_gb=8))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        html = app_client.get("/admin/system/data-fixes").text
        key = model_key(model)
        assert f'name="mn_{key}|dell"' in html and f'name="mn_{key}|hp"' in html
        assert "Same model under 2 makes" in html
        r = app_client.get("/admin/system/model-name-fix/export", params={"model": model, "make": "Dell"})
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        lines = r.content.decode("utf-8-sig").strip().splitlines()
        assert lines[0].startswith("Tag Number,Make,Model") and "PNA" in lines[0] and "Scrap" in lines[0]
        assert len(lines) == 3                                   # the 2 Dell tags only
        assert any(",Yes," in ln for ln in lines[1:])            # the scrapped one says Scrap = Yes
        # Apply on the Dell row renames only the Dell tags
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/admin/system/model-name-fix",
                            data={"csrf_token": csrf, "ajax": "1", f"mn_{key}|dell": f"{model} Pro",
                                  f"mn_{key}|hp": model}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["ok"], r.text
        got = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print(sorted(tuple(x) for x in (await db.execute(text("select brand, model from devices where barcode like 'ITMK{sfx}%'"))).all()))
asyncio.run(main())
""").splitlines()[-1]
        assert got == str(sorted([("Dell", f"{model} Pro"), ("Dell", f"{model} Pro"), ("HP", model)])), got
        # different names per Make -> nothing remembered for later saves
        ov = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select count(*) from master_data where category='model_spelling' and value ilike '%{sfx}%'"))).scalar())
asyncio.run(main())
""").splitlines()[-1]
        assert ov == "0", ov
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode like 'ITMK{sfx}%'"))
        await db.execute(text("delete from master_data where category='model_spelling' and value ilike '%{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
