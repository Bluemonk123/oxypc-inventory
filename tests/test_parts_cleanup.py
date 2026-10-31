"""Parts cleanup (2026-10-31): for one Part Name + Make + Model —
highest price to every row; a no-crate row merges into the single filled
crate of the same Type (stock added, requests moved, blank row trashed);
Harvest never merges into New; crates one character apart merge only when
the admin picks it."""
import pathlib
import subprocess
import sys
import uuid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_cleanup_prices_blank_merge_and_typo():
    name = "ITCLEAN" + uuid.uuid4().hex[:6].upper()
    out = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, select
from database import AsyncSessionLocal
from models.device import Device
from models.spare_parts import SparePart
from models.part_request import PartRequest
from services.parts_stock import cleanup_plan, cleanup_apply
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("insert into app_settings (key, value) values ('parts_stock_mode', 'row') "
                              "on conflict (key) do update set value = 'row'"))
        def sp(code, src, crate, qty, price):
            p = SparePart(part_code=code, name="{name}", category="X", make="Dell", model="E1", source=src,
                          crate_number=crate, qty_in_stock=qty, unit_price=price, min_stock_alert=0)
            db.add(p); return p
        a = sp("{name[-6:]}A1", "new", "C-1", 10, 100)
        b = sp("{name[-6:]}B1", "new", None, 4, 150)
        h = sp("{name[-6:]}H1", "harvest", None, 2, 0)
        t1 = sp("{name[-6:]}T1", "new", "SN009", 1, 100)
        t2 = sp("{name[-6:]}T2", "new", "N009", 3, 100)
        await db.flush()
        dev = (await db.execute(select(Device).limit(1))).scalars().first()
        db.add(PartRequest(part_id=b.id, part_name="{name}", request_type="new", status="received",
                           qty_handed_over=1, device_id=dev.id))
        await db.commit()
        plan = await cleanup_plan(db)
        mine = lambda xs: [x for x in xs if x["part"].startswith("{name}")]
        print(len(mine(plan["prices"])), len(mine(plan["merges"])), len(mine(plan["unresolved"])), len(mine(plan["typos"])))
        # blank New has 2 filled crates (C-1, SN009, N009) -> unresolved, not merged
        await cleanup_apply(db, None, None, [(str(t1.id), str(t2.id))])
        rows = (await db.execute(select(SparePart).where(SparePart.name == "{name}").order_by(SparePart.part_code))).scalars().all()
        print(";".join(f"{{r.part_code[-2:]}}:{{r.crate_number}}:{{r.qty_in_stock}}:{{r.unit_price}}:{{r.is_trashed}}" for r in rows))
        # now only C-1 + SN009 remain filled -> blank still unresolved (2 crates); drop SN009 to make one crate
        t1.is_trashed = True; await db.commit()
        await cleanup_apply(db)
        rows = (await db.execute(select(SparePart).where(SparePart.name == "{name}", SparePart.is_trashed.isnot(True)).order_by(SparePart.part_code))).scalars().all()
        print(";".join(f"{{r.part_code[-2:]}}:{{r.crate_number}}:{{r.qty_in_stock}}:{{r.unit_price}}" for r in rows))
        pr = (await db.execute(select(PartRequest.part_id).where(PartRequest.part_name == "{name}"))).scalar()
        print(pr == a.id)
        await db.execute(text("delete from part_requests where part_name = '{name}'"))
        await db.execute(text("update spare_parts set is_trashed = true where name = '{name}'"))
        await db.execute(text("delete from app_settings where key = 'parts_stock_mode'"))
        await db.commit()
asyncio.run(main())
""")
    lines = out.splitlines()
    assert lines[0] == "4 0 2 1", out          # A, H, T1, T2 below 150; no merge (several crates); typo pair
    assert lines[1] == ("A1:C-1:10:150:False;B1:None:4:150:False;H1:None:2:150:False;"
                        "T1:SN009:4:150:False;T2:N009:0:150:True"), out
    assert lines[2] == "A1:C-1:14:150;H1:None:2:150", out   # blank New merged into C-1; Harvest stays apart
    assert lines[3] == "True", out
