"""Pre-Sale QC entry (2026-11-22, Pankaj):
- Pre-Sale QC page "Add Tag" (primary, after Upload Tags) and Sales
  Coordinator "Add Tags" open the same modal (_presale_add_modal.html): scan /
  search tags, no Stage dropdown, Cancel / Add Tags (N); Notes on Sales
  Coordinator only. Ready for Sale page has no Add Tag button.
- Every way in (/pre-sale-iqc/assign, Production Manager Move to Stage) puts a
  tag in Ready for Sale and in the Showroom when it isn't — from any stage
  (even Sold) or location, nothing refused; both moves are in its history."""
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


def test_pre_sale_add_tag(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    ready, repair, sold, pm = f"PSAT{sfx}", f"PSAU{sfx}", f"PSAV{sfx}", f"PSAW{sfx}"
    tags = (ready, repair, sold, pm)
    made_loc = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.location import StorageLocation, ZoneType, UnitType
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        made = False
        if not (await db.execute(select(StorageLocation.id).where(StorageLocation.unit_id == "SHOWROOM-FLOOR"))).scalar():
            db.add(StorageLocation(zone=ZoneType.showroom, unit_type=UnitType.floor, unit_id="SHOWROOM-FLOOR",
                                   description="Showroom floor (test)"))
            made = True
        for bc, st in (("{ready}", S.ready_to_sale), ("{repair}", S.l1), ("{sold}", S.sold), ("{pm}", S.final_qc)):
            db.add(Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=st, is_active=True))
        await db.commit()
        print("made" if made else "kept")
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        page = app_client.get("/pre-sale-iqc").text
        assert page.index("Upload Tags") < page.index('id="psAddTagBtn"') < page.index('id="readyBulkBtns"')
        modal = page.split('id="psAddModal"', 1)[1].split('<!-- Bulk Upload Tags modal', 1)[0]
        assert "<select" not in modal and "Add Tags (" in modal and "Cancel" in modal
        assert 'id="psAdd_notes"' not in modal
        assert 'id="psAddTagBtn"' not in app_client.get("/sales/ready").text
        sc = app_client.get("/sales-coordinator").text
        assert 'id="psAddModal"' in sc and 'id="psAdd_notes"' not in sc and "data-presale-add" in sc   # notes removed 2026-11-23
        assert 'id="scAddSearch"' not in sc                       # the old Add Tags modal is gone
        csrf = app_client.cookies.get("csrf_token", "")
        h = {"x-csrf-token": csrf}
        d = app_client.post("/pre-sale-iqc/assign", data={"barcodes": f"{ready},{repair},{sold}", "csrf_token": csrf},
                            headers=h).json()
        assert sorted(d["assigned_tags"]) == sorted([ready, repair, sold])
        assert sorted(d["moved_tags"]) == sorted([repair, sold])
        assert sorted(d["relocated_tags"]) == sorted([ready, repair, sold]) and d["not_ready_tags"] == []
        d2 = app_client.post("/pre-sale-iqc/assign", data={"barcodes": ready, "csrf_token": csrf}, headers=h).json()
        assert d2["already_tags"] == [ready] and d2["assigned"] == 0 and d2["relocated_tags"] == []
        # Production Manager Move to Stage -> Pre-Sale QC: same check
        r = app_client.post("/devices/change-engineer", data={"barcode": pm, "target_stage": "pre_sale_iqc",
                                                              "csrf_token": csrf}, headers=h)
        assert r.status_code == 200 and r.json()["ok"], r.text
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "select d.barcode, d.current_stage, coalesce(l.unit_id, '-') from devices d "
            "left join storage_locations l on l.id = d.location_id "
            "where d.barcode in ('{ready}', '{repair}', '{sold}', '{pm}') order by d.barcode"))).all()
        print(";".join("|".join(map(str, r)) for r in rows))
asyncio.run(main())
""")
        got = {x.split("|")[0]: x.split("|", 1)[1] for x in out.split(";")}
        assert got[ready] == "ready_to_sale|SHOWROOM-FLOOR"
        assert got[repair] == "ready_to_sale|SHOWROOM-FLOOR"
        assert got[pm] == "ready_to_sale|SHOWROOM-FLOOR"
        assert got[sold] == "ready_to_sale|SHOWROOM-FLOOR"
        hist = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text(
            "select (select count(*) from stage_movements m join devices d on d.id = m.device_id "
            "        where d.barcode = '{sold}' and m.to_stage = 'ready_to_sale'), "
            "       (select count(*) from device_location_logs l join devices d on d.id = l.device_id "
            "        where d.barcode = '{sold}')"))).one())
asyncio.run(main())
""")
        assert hist == "(1, 1)"                  # the stage move and the Showroom move are both in its history
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in {tags!r}"
        for t in ("work_orders", "stage_movements", "device_location_logs"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode in {tags!r}"))
        if "{made_loc}" == "made":       # the test's own Showroom location, gone again
            await db.execute(text("delete from storage_locations where unit_id = 'SHOWROOM-FLOOR' "
                                  "and not exists (select 1 from device_location_logs l join storage_locations s "
                                  "on s.id = l.location_id where s.unit_id = 'SHOWROOM-FLOOR')"))
        await db.commit()
asyncio.run(main())
""")
