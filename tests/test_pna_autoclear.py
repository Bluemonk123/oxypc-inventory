"""PNA auto-clear (2026-11-22, Pankaj): a tag's active PNA parts clear when it
passes L1/L2 (L1/L2 -> Stress), reaches Ready for Sale or is Sold; L1/L2 ->
L3/L4 keeps them. Cleared, not deleted (PNA History keeps them)."""
import json
import pathlib
import subprocess
import sys
import uuid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def test_pna_autoclear_on_stage_moves():
    sfx = uuid.uuid4().hex[:6].upper()
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, json, sys, uuid
sys.path.insert(0, r"{ROOT}")
import services.pna_autoclear  # noqa: F401  (main.py imports it)
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.pna_part import DevicePNAPart
from services.repair_location import current_user_name

async def main():
    current_user_name.set("Tester")
    out = {{}}
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        devs = {{}}
        for k, st in (("a", S.l1), ("b", S.stock_in), ("c", S.l2)):
            d = Device(id=uuid.uuid4(), barcode="PNAC" + k.upper() + "{sfx}", lot_id=lot, current_stage=st)
            db.add(d); await db.flush()
            db.add(DevicePNAPart(device_id=d.id, barcode=d.barcode, part_name="RAM", source="l1",
                                 marked_by="t", is_active=True))
            devs[k] = d
        await db.commit()

        async def active(k):
            await db.commit()
            return (await db.execute(select(DevicePNAPart.is_active, DevicePNAPart.cleared_by)
                                     .where(DevicePNAPart.device_id == devs[k].id))).one()
        devs["a"].current_stage = S.l3            # L1/L2 -> L3/L4: still in repair
        out["to_l3"] = list(await active("a"))
        devs["a"].current_stage = S.l1
        await db.flush()
        devs["a"].current_stage = S.qc_check      # passed L1/L2
        out["to_stress"] = list(await active("a"))
        devs["b"].current_stage = S.ready_to_sale
        out["to_ready"] = list(await active("b"))
        devs["c"].current_stage = S.sold          # customer return repaired back to Sold
        out["to_sold"] = list(await active("c"))
        await db.execute(text("delete from device_pna_parts where barcode like 'PNAC%{sfx}'"))
        await db.execute(text("delete from devices where barcode like 'PNAC%{sfx}'"))
        await db.commit()
    print(json.dumps(out))

asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["to_l3"] == [True, None]
    assert o["to_stress"] == [False, "Tester (auto: passed L1/L2)"]
    assert o["to_ready"] == [False, "Tester (auto: ready for sale)"]
    assert o["to_sold"] == [False, "Tester (auto: sold)"]
