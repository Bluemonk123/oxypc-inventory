"""Manage Lots rework (2026-11-23, Pankaj): no GRN step — upload the Asset
IQC sheet straight away, then pick the Device Category for the new tags;
Lots / Models / Tags tabs from the uploaded tags; Set Price (Lots tab only)
also sets the Sub-lot Number, which partners see instead of the lot number."""
import pathlib
import re
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2500:]
    return r.stdout.strip()


def test_manage_lots_rework(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:5].upper()
    lot_no, t1, t2 = f"9{sfx}", f"MLA{sfx}", f"MLB{sfx}"
    csv = (f"Tag No,Grade,LOT Number,Make,Model,CPU,Generation,RAM,RAM (GB)\n"
           f"{t1},A,{lot_no},Dell,Latitude 5490,i5,8th,Yes,8\n"
           f"{t2},B,{lot_no},Dell,Latitude 5490,i5,8th,Yes,8\n")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        csrf = app_client.cookies.get("csrf_token", "")
        h = {"x-csrf-token": csrf}
        page = app_client.get("/trade-partner/manage-lots").text
        assert 'data-tab="lots"' in page and 'data-tab="models"' in page and 'data-tab="tags"' in page
        assert "GRN &amp; Lot" not in page and "_post_iqc" not in page
        r = app_client.post("/trade-partner/manage-lots/asset-iqc-upload", headers=h, data={"csrf_token": csrf},
                            files={"file": ("iqc.csv", csv.encode(), "text/csv")}, follow_redirects=False)
        assert r.status_code == 302, r.text[:300]
        loc = r.headers["location"]
        m = re.search(r"ask_category=([0-9a-f-]{36})", loc)
        assert m, loc
        page = app_client.get(loc).text
        assert 'id="askCategoryModal"' in page and "<b>2</b> new tag(s)" in page
        r = app_client.post("/trade-partner/manage-lots/set-category", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "upload_id": m.group(1), "category": "Laptop"})
        assert r.status_code == 302 and "2+tag" in r.headers["location"], r.headers["location"]
        lot_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select id from lots where lot_number ilike '%{lot_no}'"))).scalar())
asyncio.run(main())
""")
        # Sub-lot is given automatically at upload (Lot-NNN); Set Price also sets the Device Category
        sub = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select string_agg(distinct sub_lot_number, ',') from devices where lot_id = '{lot_id}'"))).scalar())
asyncio.run(main())
""")
        assert re.fullmatch(r"Lot-\d{3,}", sub), sub
        r = app_client.post("/trade-partner/manage-lots/set-price", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "lot_id": lot_id, "price": "50000", "category": "Desktop"})
        assert "success" in r.headers["location"], r.headers["location"]
        page = app_client.get("/trade-partner/manage-lots").text
        lots_tab = page.split('id="lotsTab"', 1)[1].split('id="modelsTab"', 1)[0]
        assert sub in lots_tab and "Desktop 2" in lots_tab and "set-price-btn" in lots_tab
        assert 'id="spCat"' in page and 'id="spSub"' in page
        tags_tab = page.split('id="assetIqcTab"', 1)[1]
        assert t1 in tags_tab and "tag-set-price-btn" in tags_tab.split("setPriceModal")[0]   # tag Set Price too (2026-11-27)
        models_tab = page.split('id="modelsTab"', 1)[1].split('id="assetIqcTab"', 1)[0]
        assert "Latitude 5490" in models_tab
        out = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from routers.partner_portal import partner_lot_labels
async def main():
    async with AsyncSessionLocal() as db:
        lid = uuid.UUID("{lot_id}")
        print((await partner_lot_labels(db, [lid]))[lid])
        print((await db.execute(text("select string_agg(distinct sub_category, ',') from devices where lot_id = :l"), {{"l": lid}})).scalar())
asyncio.run(main())
""").splitlines()
        assert out == [sub, "Desktop"], out
        # Partner catalog: only Manage Lots lots, shown by Sub-lot (never the lot number)
        from main import app
        from auth.partner_auth import get_current_partner
        from types import SimpleNamespace
        dealer_id = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.dealers import Dealer
async def main():
    async with AsyncSessionLocal() as db:
        d = Dealer(id=uuid.uuid4(), dealer_code="MLP{sfx}", business_name="ML Partner {sfx}", portal_enabled=True)
        db.add(d); await db.commit(); print(d.id)
asyncio.run(main())
""")
        async def _partner():
            from database import AsyncSessionLocal
            from models.dealers import Dealer
            async with AsyncSessionLocal() as db:
                return await db.get(Dealer, uuid.UUID(dealer_id))
        app.dependency_overrides[get_current_partner] = _partner
        try:
            cat = app_client.get("/partner/catalog")
            assert cat.status_code == 200, cat.text[:300]
            html = cat.text
            assert sub in html and f"EPT-{lot_no}" not in html and ">Sub-Lot<" in html
            assert "LOT-2048" not in html                    # live lots are not in the catalog
        finally:
            app.dependency_overrides.pop(get_current_partner, None)
            _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from dealers where id = '{dealer_id}'")); await db.commit()
asyncio.run(main())
""")
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in ('{t1}', '{t2}')"
        for t in ("iqc_inspections", "stage_movements", "device_location_logs", "iqc_snapshots", "notes_history", "stock_transfers"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode in ('{t1}', '{t2}')"))
        await db.execute(text("delete from lots where lot_number ilike '%{lot_no}'"))
        await db.commit()
asyncio.run(main())
""")
