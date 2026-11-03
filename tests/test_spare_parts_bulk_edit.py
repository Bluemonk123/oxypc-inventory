"""Part Master Bulk Edit (2026-11-03): Make / Category / Unit Price on the
ticked rows; a blank field is left as it is; price needs Pricing Add; a row
that would duplicate another crate row is skipped."""
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


def test_bulk_edit_parts(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    out = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.spare_parts import SparePart
async def main():
    async with AsyncSessionLocal() as db:
        mk = lambda code, make, crate: SparePart(id=uuid.uuid4(), part_code=code, name="BE Part {sfx}", category="RAM",
                                                 make=make, model="M1", source="new", crate_number=crate,
                                                 unit_price=100, qty_in_stock=1, min_stock_alert=0)
        a, b, c = mk("BEA{sfx}", "Dell", "CR-A{sfx}"), mk("BEB{sfx}", "Dell", "CR-B{sfx}"), mk("BEC{sfx}", "HP", "CR-A{sfx}")
        db.add_all([a, b, c]); await db.commit()
        print(a.id, b.id, c.id)
asyncio.run(main())
""")
    a, b, c = out.split()
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        html = app_client.get("/spare-parts").text
        assert 'id="bulkEdit_partsTable"' in html and 'id="bulkEditModal"' in html and 'id="bePrice"' in html
        assert html.index('id="bulkEdit_partsTable"') < html.index('data-bs-target="#addPartModal"')

        def post(**data):
            return app_client.post("/spare-parts/bulk-edit", data={"csrf_token": csrf, **data},
                                   headers={"X-CSRF-Token": csrf}).json()
        assert post(ids=[a, b])["error"] == "Fill in at least one field."
        # Category + price on a and b; make blank = unchanged
        d = post(ids=[a, b], category="Battery", unit_price="250")
        assert d["ok"] and d["updated"] == 2 and d["skipped"] == []
        # c -> make Dell would duplicate a (same name/make/model/source/crate) -> skipped
        d = post(ids=[c], make="Dell")
        assert d["ok"] and d["updated"] == 0 and len(d["skipped"]) == 1
        vals = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.spare_parts import SparePart
async def main():
    async with AsyncSessionLocal() as db:
        for pid in ("{a}", "{b}", "{c}"):
            p = (await db.execute(select(SparePart).where(SparePart.id == pid))).scalar_one()
            print(p.make, p.category, float(p.unit_price))
asyncio.run(main())
""").splitlines()
        assert vals == ["Dell Battery 250.0", "Dell Battery 250.0", "HP RAM 100.0"], vals
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from spare_parts where part_code in ('BEA{sfx}', 'BEB{sfx}', 'BEC{sfx}')"))
        await db.commit()
asyncio.run(main())
""")
