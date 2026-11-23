"""Production Manager page — per-tile "Summary" CSV downloads.

Every summary tile (except PNA, which has its own endpoint) links to
GET /trc-production/summary/{tile}; the CSV holds exactly the tags the tile
counts (shared criterion in routers.stock._prod_tile_criteria).
"""
import csv
import io
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.test_production_manager_summary_tiles import _cleanup, _run, _seed

# tile -> (stage that belongs, stage that does not)
TILES = {
    "at-you": ("trc_production", "l3"),
    "l1-l2": ("l2", "trc_production"),
    "l3-l4": ("l3", "l1"),
    "stress": ("qc_check", "l3"),
    "cosmetic": ("painting", "l1"),
    "final-qc": ("final_qc", "final_qc_fail_hold"),     # Final QC only (2026-11-22)
    "returned": ("l1", "l1"),
}


def _return_on_repair_card(barcode, with_return):
    """2026-11-22: Tags Returned = an L1/L2 WorkID today and a customer return
    (not rejected) logged today."""
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{__import__('tests.test_production_manager_summary_tiles', fromlist=['ROOT']).ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Return
from models.work_order import WorkOrder
from utils.timezone import app_now

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        db.add(WorkOrder(work_id="R" + uuid.uuid4().hex[:11], device_id=dev.id, barcode=dev.barcode, stage="l1",
                         status="pending", created_at=app_now()))
        if {with_return}:
            db.add(Return(device_id=dev.id, return_date=app_now(), approval_status="approved", processed_by="t"))
        dev.tag_return_status = "Return for Repair"
        await db.commit()

asyncio.run(main())
""")


def _drop_return_rows(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{__import__('tests.test_production_manager_summary_tiles', fromlist=['ROOT']).ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode = '{barcode}'"
        await db.execute(text(f"delete from returns where device_id in ({{ids}})"))
        await db.execute(text(f"delete from work_orders where device_id in ({{ids}})"))
        await db.commit()

asyncio.run(main())
""")


def _set_return_status(barcode, status):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{__import__('tests.test_production_manager_summary_tiles', fromlist=['ROOT']).ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        dev.tag_return_status = "{status}"
        await db.commit()

asyncio.run(main())
""")


def _rows(resp):
    return list(csv.reader(io.StringIO(resp.text)))


def test_each_tile_summary_csv(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    seeded = {}
    try:
        for tile, (yes_stage, no_stage) in TILES.items():
            key = tile.replace("-", "")[:6].upper()
            yes_bc, no_bc = f"ITSUMY{key}{suffix}", f"ITSUMN{key}{suffix}"
            _seed(yes_bc, yes_stage)
            _seed(no_bc, no_stage)
            seeded[tile] = (yes_bc, no_bc)
        # Returned tile: both on the L1/L2 card today; only the first has a return logged today.
        _return_on_repair_card(seeded["returned"][0], True)
        _return_on_repair_card(seeded["returned"][1], False)

        username, password = make_user("admin")
        _login(app_client, username, password)

        for tile, (yes_bc, no_bc) in seeded.items():
            r = app_client.get(f"/trc-production/summary/{tile}", follow_redirects=True)
            assert r.status_code == 200, tile
            assert r.headers["content-type"].startswith("text/csv"), tile
            assert f"trc_{tile.replace('-', '_')}_summary.csv" in r.headers["content-disposition"]
            rows = _rows(r)
            header = ["Tag Number", "Brand", "Model", "Lot Number", "Stage", "Updated At"]
            if tile == "returned":
                header.append("Return Status")
            assert rows[0] == header, tile
            tags = {row[0] for row in rows[1:]}
            assert yes_bc in tags, tile
            assert no_bc not in tags, tile
            if tile == "returned":
                row = next(x for x in rows[1:] if x[0] == yes_bc)
                assert row[-1] == "Return for Repair"
    finally:
        for yes_bc, no_bc in seeded.values():
            _drop_return_rows(yes_bc)
            _drop_return_rows(no_bc)
            _cleanup(yes_bc)
            _cleanup(no_bc)


def test_unknown_tile_is_404(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/trc-production/summary/bogus", follow_redirects=True)
    assert r.status_code == 404


def test_page_has_summary_link_for_every_tile(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/trc-production", follow_redirects=True).text
    for tile in TILES:
        # Tags Returned's link carries the page's Date filter (2026-11-22)
        assert (f'href="/trc-production/summary/{tile}"' in html
                or f'href="/trc-production/summary/{tile}?' in html), tile
    assert 'href="/trc-production/pna-summary"' in html


def test_pna_summary_one_row_per_tag(app_client, make_user):  # noqa: F811
    """2026-11-22 (Pankaj): Tag Number, Lot, Brand, Model, Stage, Marked Date
    (oldest), Marked By (each once, " / "), Parts Count, Part Name (" / ")."""
    suffix = uuid.uuid4().hex[:6]
    t1, t2 = f"ITPNA1{suffix}", f"ITPNA2{suffix}"
    _seed(t1, "l1")
    _seed(t2, "l3")
    root = __import__('tests.test_production_manager_summary_tiles', fromlist=['ROOT']).ROOT
    _run(f"""
import asyncio, sys
from datetime import datetime
sys.path.insert(0, r"{root}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.pna_part import DevicePNAPart

async def main():
    async with AsyncSessionLocal() as db:
        for bc, parts in (("{t1}", [("RAM", "eng_x", datetime(2026, 11, 20, 10)), ("Battery", "eng_y", datetime(2026, 11, 18, 9)),
                                    ("Keyboard", "eng_x", datetime(2026, 11, 21, 9))]),
                          ("{t2}", [("Screen", "eng_y", datetime(2026, 11, 19, 9))])):
            d = (await db.execute(select(Device).where(Device.barcode == bc))).scalar_one()
            for name, who, at in parts:
                db.add(DevicePNAPart(device_id=d.id, barcode=bc, part_name=name, source="l1",
                                     marked_by=who, marked_at=at, is_active=True))
        await db.commit()

asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        rows = _rows(app_client.get("/trc-production/pna-summary", follow_redirects=True))
        assert rows[0] == ["Tag Number", "Lot", "Brand", "Model", "Stage", "Marked Date", "Marked By",
                           "Parts Count", "Part Name"]
        mine = {r[0]: r for r in rows[1:] if r[0] in (t1, t2)}
        assert set(mine) == {t1, t2} and sum(1 for r in rows[1:] if r[0] == t1) == 1
        r1 = mine[t1]
        assert r1[5].startswith("2026-11-18")                    # oldest mark (export shows the date)
        assert r1[6] == "eng_x / eng_y" or r1[6] == "eng_y / eng_x"
        assert r1[7] == "3" and set(r1[8].split(" / ")) == {"RAM", "Battery", "Keyboard"}
        assert mine[t2][7] == "1" and mine[t2][8] == "Screen"
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{root}")
from sqlalchemy import text
from database import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from device_pna_parts where barcode in ('{t1}', '{t2}')"))
        await db.commit()

asyncio.run(main())
""")
        _cleanup(t1)
        _cleanup(t2)
