"""Device Edit identity rows (2026-11-24, Pankaj): same two rows as Register
Device — Tag · Lot · Color / GRN · Invoice · Location ID · Entity, each on the
saved value. Saving keeps the Location ID (it used to be blanked on every edit
because the form had no Location field) and saves Invoice + Entity."""
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


def _q(sql):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = (await db.execute(text({sql!r}))).first()
        print("|".join("" if x is None else str(x) for x in (r or [])))
        await db.commit()
asyncio.run(main())
""")


def test_device_edit_identity_rows(app_client, make_user):  # noqa: F811
    tag = f"EDID{uuid.uuid4().hex[:6].upper()}"
    loc = _q("select id from storage_locations order by unit_id limit 1")
    loc2 = _q("select id from storage_locations order by unit_id desc limit 1")
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=S.l1, is_active=True,
                      grn_number="000000000777", invoice_number="INV-OLD", floor="F2",
                      location_id={('uuid.UUID("' + loc + '")') if loc else 'None'}, entity="OxyPC Computers"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get(f"/devices/{tag}/edit").text
        ident = html.split("Identity</p>", 1)[1].split("Hardware Specification", 1)[0]
        order = ['id="editBarcode"', 'id="lot_id"', 'name="color"', 'id="grn_number"', 'id="invoice_number"',
                 'id="iqcLocation"', 'id="iqcEntity"']
        pos = [ident.index(x) for x in order]
        assert pos == sorted(pos), pos
        assert '<option value="000000000777" selected>' in ident and 'value="INV-OLD"' in ident
        if loc:
            assert f'<option value="{loc}" selected>' in ident
        csrf = app_client.cookies.get("csrf_token", "")
        base = {"csrf_token": csrf, "barcode_new": tag, "brand": "Dell", "model": "Latitude 5490"}
        # an old-style save that sends no location / floor keeps them (used to blank them)
        r = app_client.post(f"/devices/{tag}/edit", data=base, headers={"x-csrf-token": csrf}, follow_redirects=False)
        assert r.status_code in (302, 303), r.text[:300]
        assert _q(f"select coalesce(location_id::text,''), floor from devices where barcode = '{tag}'") == f"{loc}|F2"
        # the new rows save
        data = {**base, "grn_number": "000000000888", "invoice_number": "INV-NEW", "location_id": loc2 or "",
                "entity": "OxyPC Computers"}
        r = app_client.post(f"/devices/{tag}/edit", data=data, headers={"x-csrf-token": csrf}, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert _q(f"select grn_number, invoice_number, coalesce(location_id::text,'') from devices where barcode = '{tag}'") \
            == f"000000000888|INV-NEW|{loc2}"
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, select
from database import AsyncSessionLocal
from utils.fk_purge import purge_references
async def main():
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(text("select id from devices where barcode = '{tag}'"))).scalars().all()
        if ids:
            await purge_references(db, "devices", ids)
            await db.execute(text("delete from devices where barcode = '{tag}'"))
        await db.commit()
asyncio.run(main())
""")


def test_device_edit_location_from_location_log(app_client, make_user):  # noqa: F811
    """2026-11-25: a tag placed through the location flow has only a location
    log entry (device.location_id empty). Device Detail shows that location;
    Device Edit now starts its Location ID dropdown on the same one."""
    tag = f"EDLL{uuid.uuid4().hex[:6].upper()}"
    unit = f"T-{tag}"
    u, p = make_user("admin")
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.location import DeviceLocationLog, LocationAction, StorageLocation, ZoneType, UnitType
async def main():
    async with AsyncSessionLocal() as db:
        sl = StorageLocation(zone=list(ZoneType)[0], unit_type=list(UnitType)[0], unit_id="{unit}")
        db.add(sl)
        await db.flush()
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        actor = (await db.execute(text("select id from users where username = '{u}'"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=S.l1, is_active=True, location_id=None)
        db.add(d)
        await db.flush()
        db.add(DeviceLocationLog(device_id=d.id, location_id=sl.id,
                                 action=list(LocationAction)[0], actor_id=actor, actor_name="t"))
        await db.commit()
asyncio.run(main())
""")
    try:
        _login(app_client, u, p)
        loc = _q(f"select id from storage_locations where unit_id = '{unit}'")
        html = app_client.get(f"/devices/{tag}/edit").text
        assert f'<option value="{loc}" selected>{unit}</option>' in html
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from utils.fk_purge import purge_references
async def main():
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(text("select id from devices where barcode = '{tag}'"))).scalars().all()
        if ids:
            await purge_references(db, "devices", ids)
            await db.execute(text("delete from devices where barcode = '{tag}'"))
        await db.execute(text("delete from storage_locations where unit_id = '{unit}'"))
        await db.commit()
asyncio.run(main())
""")
