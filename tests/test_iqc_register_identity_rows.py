"""Register Device — IQC identity rows (2026-11-24, Pankaj):
- Row 1: Tag Number · Lot Number · Color; Row 2: GRN Number · Invoice Number · Location ID · Entity.
- Lot from the tag's first 4–6 characters (longest exact lot number wins).
- GRN Number is a dropdown of GRN records; picking one fills Invoice + Location ID.
- Entity is a dropdown, starting on the user's own entity."""
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


def test_iqc_register_identity_rows(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:2].upper()
    short, long_ = f"9{sfx}7", f"9{sfx}7Q1"          # 4- and 6-character lots
    grn_no = "9" + uuid.uuid4().hex[:11].upper()
    ids = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.lot import Lot
from models.grn_import import GRNImport
async def main():
    async with AsyncSessionLocal() as db:
        a = Lot(id=uuid.uuid4(), lot_number="{short}", supplier_name="S", buying_price=0, qty=1, purchase_date=__import__("datetime").date.today())
        b = Lot(id=uuid.uuid4(), lot_number="{long_}", supplier_name="S", buying_price=0, qty=1, purchase_date=__import__("datetime").date.today())
        loc = (await db.execute(text("select id from storage_locations limit 1"))).scalar()
        g = GRNImport(id=uuid.uuid4(), grn_number="{grn_no}", invoice_number="INV-{sfx}", location_id=loc)
        db.add_all([a, b, g]); await db.commit()
        print(a.id, b.id, loc or "")
asyncio.run(main())
""")
    a_id, b_id, loc_id = (ids.split() + [""])[:3]
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        ent = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from utils.master_data import master_options
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        from utils.master_data import refresh_master_cache
        await refresh_master_cache(db)
        opts = master_options("entity") or []
        e = opts[-1] if opts else ""
        await db.execute(text("update users set entity = :e where username = :u"), {{"e": e, "u": "{u}"}})
        await db.commit(); print(e)
asyncio.run(main())
""")
        _login(app_client, u, p)
        html = app_client.get("/iqc/new").text
        ident = html.split("Identity</p>", 1)[1].split("Hardware Specification", 1)[0]
        assert ident.index('id="iqcBarcode"') < ident.index('id="lot_id"') < ident.index('name="color"')
        assert ident.index('name="color"') < ident.index('id="grn_number"') < ident.index('id="invoice_number"') \
            < ident.index('id="iqcLocation"') < ident.index('id="iqcEntity"')
        assert '<select name="grn_number"' in ident and 'type="radio"' not in ident
        if ent:
            assert f'<option value="{{ent}}" selected>' in ident, ent
        # lot from the first 4–6 characters: the longest match wins
        assert app_client.get(f"/api/lot-meta?tag_prefix={long_}0526L01").json()["lot_id"] == b_id
        assert app_client.get(f"/api/lot-meta?tag_prefix={short}XX").json()["lot_id"] == a_id
        o = app_client.get("/api/iqc-form-options").json()
        g = next(x for x in o["grns"] if x["grn"] == grn_no)
        assert g["invoice"] == f"INV-{sfx}" and g["location_id"] == loc_id
        if loc_id:
            assert any(l["id"] == loc_id for l in o["locations"])
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from grn_imports where grn_number = '{grn_no}'"))
        await db.execute(text("delete from lots where lot_number in ('{short}', '{long_}')"))
        await db.commit()
asyncio.run(main())
""")
