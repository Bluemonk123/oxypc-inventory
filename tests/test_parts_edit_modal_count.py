"""Parts Manager (2026-11-05): Edit opens in a modal (?modal=1 = the form
alone), Update Count button removed, and Edit's In Stock is the live count —
saving a new number makes In Stock exactly that (audited), like Update Count."""
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


def test_edit_modal_sets_live_count(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    pid = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.spare_parts import SparePart
async def main():
    async with AsyncSessionLocal() as db:
        sp = SparePart(id=uuid.uuid4(), part_code="ITPE{sfx}", name="ITPE-Part-{sfx}", category="ITPE",
                       unit_price=100, qty_in_stock=10, min_stock_alert=0, crate_number="ITPEC{sfx}")
        db.add(sp); await db.commit(); print(sp.id)
asyncio.run(main())
""").splitlines()[-1]
    live = lambda: int(_run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.spare_parts import SparePart
from services.parts_stock import live_map
async def main():
    async with AsyncSessionLocal() as db:
        sp = (await db.execute(select(SparePart).where(SparePart.part_code == "ITPE{sfx}"))).scalar_one()
        print((await live_map(db, [sp]))[str(sp.id)])
asyncio.run(main())
""").splitlines()[-1])
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/spare-parts").text
        assert "Update Count</button>" not in html
        assert f'data-edit-part="{pid}"' in html and 'id="editPartModal"' in html
        frag = app_client.get(f"/spare-parts/{pid}/edit", params={"modal": "1"}).text
        assert "<form" in frag and "<html" not in frag.lower() and 'name="shown_stock"' in frag
        assert 'data-bs-dismiss="modal"' in frag
        page = app_client.get(f"/spare-parts/{pid}/edit").text
        assert "<html" in page.lower() and 'name="qty_in_stock"' in page
        shown = live()
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post(f"/spare-parts/{pid}/edit", follow_redirects=False,
                            data={"csrf_token": csrf, "name": f"ITPE-Part-{sfx}", "category": "ITPE",
                                  "unit_price": "100", "qty_in_stock": str(shown + 3), "shown_stock": str(shown),
                                  "count_reason": "Stock-take", "min_stock_alert": "0",
                                  "crate_number": f"ITPEC{sfx}"},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 302 and "success" in r.headers["location"]
        assert live() == shown + 3
        n = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select count(*) from audit_logs where action='PART_COUNT_UPDATED' and record_id=:i"), {{"i": "{pid}"}})).scalar())
asyncio.run(main())
""").splitlines()[-1]
        assert int(n) == 1
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from spare_parts_ledger where part_id in (select id from spare_parts where part_code='ITPE{sfx}')"))
        await db.execute(text("delete from spare_parts where part_code='ITPE{sfx}'"))
        await db.commit()
asyncio.run(main())
""")
