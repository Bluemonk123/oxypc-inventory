"""Repair stages put the tag on TRC Floor (2026-10-31): entering L1/L2 (or
any repair stage) from outside the repair line logs a move to TRC-FLOOR;
moves inside the line don't log again; a tag already on TRC Floor isn't
re-logged."""
import pathlib
import subprocess
import sys
import uuid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def test_repair_entry_moves_to_trc_floor():
    bc = "ITRL" + uuid.uuid4().hex[:8].upper()
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
import services.repair_location  # noqa
from sqlalchemy import text, select
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.location import DeviceLocationLog, LocationAction, StorageLocation, ZoneType, UnitType
async def main():
    async with AsyncSessionLocal() as db:
        made = []
        for uid_, z in (("TRC-FLOOR", ZoneType.workshop), ("IT-SHOWROOM-T", ZoneType.showroom)):
            if not (await db.execute(select(StorageLocation.id).where(StorageLocation.unit_id == uid_))).scalar():
                sl = StorageLocation(zone=z, unit_type=UnitType.floor, unit_id=uid_); db.add(sl); made.append(uid_)
        await db.commit()
        trc = (await db.execute(select(StorageLocation.id).where(StorageLocation.unit_id == 'TRC-FLOOR'))).scalar()
        show = (await db.execute(select(StorageLocation.id).where(StorageLocation.zone == ZoneType.showroom))).scalars().first()
        import services.repair_location as rl; rl._cache.clear()
        uid = (await db.execute(text("select id from users limit 1"))).scalar()
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        if trc is None or show is None:
            print("SKIP"); return
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.stock_in, device_type="Laptop")
        db.add(d); await db.flush()
        db.add(DeviceLocationLog(device_id=d.id, location_id=show, action=LocationAction.assigned, actor_id=uid))
        await db.commit()
        n = lambda: db.execute(text("select count(*) from device_location_logs where device_id = :d"), {{"d": d.id}})
        d.current_stage = DeviceStage.l1; await db.commit()
        a = (await n()).scalar()
        last = (await db.execute(text("select location_id from device_location_logs where device_id = :d order by logged_at desc limit 1"), {{"d": d.id}})).scalar()
        d.current_stage = DeviceStage.qc_check; await db.commit()
        b = (await n()).scalar()
        d.current_stage = DeviceStage.ready_to_sale; await db.commit()
        d.current_stage = DeviceStage.l1; await db.commit()     # already on TRC Floor -> no new row
        c = (await n()).scalar()
        print(a, str(last) == str(trc), b, c, str(d.location_id) == str(trc))
        await db.execute(text("delete from device_location_logs where device_id = :d"), {{"d": d.id}})
        await db.execute(text("delete from devices where id = :d"), {{"d": d.id}})
        for uid_ in made:
            await db.execute(text("delete from storage_locations where unit_id = :u"), {{"u": uid_}})
        await db.commit()
asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout.strip()
    if out == "SKIP":
        return
    assert out == "2 True 2 2 True", out
