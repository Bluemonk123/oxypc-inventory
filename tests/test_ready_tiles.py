"""Ready for Sale Tags tiles (2026-10-28): the six device-type tiles,
INVENTORY STOCK and GRADES > TAGS come from the Tag table's own feed
(/sales/ready/data "tiles"), so they count exactly the tags the table holds,
with its filters. Ready for Sale - Lots has the same dark tiles plus AS-IS
STOCK / GRADES > AS-IS LOTS, counted from its As-Is Lot rows. INVENTORY STOCK
links to /sales/ready, AS-IS STOCK to /sales/lots."""
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


def test_ready_tiles_follow_tag_table(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tags = {f"ITRT{sfx}A": ("Laptop", "A", False), f"ITRT{sfx}B": ("TFT Monitor", "B", True),
            f"ITRT{sfx}C": ("Laptop", "C", False)}
    seed = ", ".join(f'("{bc}", "{dt}", "{g}", {blk})' for bc, (dt, g, blk) in tags.items())
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, DeviceGrade
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for bc, dt, g, blk in [{seed}]:
            db.add(Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                          device_type=dt, grade=DeviceGrade(g), is_blocked=blk, entity="ITRT{sfx}"))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        # Entity filter narrows the table to just the seeded tags
        r = app_client.get(f"/sales/ready/data?draw=1&start=0&length=25&entity=ITRT{sfx}")
        j = r.json()
        t = j["tiles"]
        assert j["recordsFiltered"] == 3 and t["total"] == 3
        assert t["types"]["laptop"] == 2 and t["types"]["tft monitor"] == 1
        assert t["open"] == 2 and t["blocked"] == 1
        assert t["grades"] == {"A": 1, "B": 1, "C": 1}
        assert t["touch"] == 0 and t["non_touch"] == 3
        # search narrows the tiles too
        j = app_client.get(f"/sales/ready/data?draw=2&start=0&length=25&entity=ITRT{sfx}"
                           f"&search[value]=ITRT{sfx}A").json()
        assert j["tiles"]["total"] == 1 and j["tiles"]["types"] == {"laptop": 1}

        html = app_client.get("/sales/ready").text
        for s in ("Inventory Stock", "Grades &gt; Tags", 'href="/sales/ready" class="rfs-link"',
                  'data-rfs-type="laptop"', 'data-rfs-type="server"', "Touch Types"):
            assert s in html, s
        assert "As-Is Stock" not in html          # moved to Ready for Sale - Lots
        assert "rfsSizeBtns" not in html          # size switch removed

        # Ready for Sale - Lots: the same six dark tiles + AS-IS STOCK and
        # GRADES > AS-IS LOTS, counted in the page from the As-Is Lot rows.
        html = app_client.get("/sales/lots").text
        for s in ("As-Is Stock", "Grades &gt; As-Is Lots", 'href="/sales/lots" class="rfs-link"',
                  'data-rfs-type="laptop"', 'data-rfs-type="tft monitor"', "Touch Types", "data-rfs-touch",
                  "function rfsLotTiles"):
            assert s in html, s
        assert "Inventory Stock" not in html
        # Filters: same set and order as the Tags page (2026-10-28)
        bar = html.split('id="asIsLotTable"', 1)[0]
        order = [bar.index(k) for k in ('ms_val_al_entity', 'ms_val_al_dtype', 'ms_val_al_grade',
                                        'id="alTouch"', 'id="alAvail"')]
        assert order == sorted(order)
        # Export in both tabs, after the filters; Total Quantities hidden
        assert 'id="alExportBtn"' in html and 'id="amExportBtn"' in html
        assert bar.index('id="alAvail"') < bar.index('id="alExportBtn"')
        assert "{ visible: false, targets: [4] }" in html
        for q in ("al_grade=A", "al_avail=blocked", "al_avail=open&al_grade=NG"):
            assert app_client.get("/sales/lots?" + q).status_code == 200
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode like 'ITRT{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
