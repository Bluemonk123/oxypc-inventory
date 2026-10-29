"""Repair cost (2026-10-29): Labor Rates by device type (With L3/L4 when the tag
has L3/L4 history; worked-on tags only) + Packing + Freight (each On Tags / On
As-Is Lot / Both). Saved on the Additional Rates page (/admin/cost-config) and
used by the Ready for Sale Min Selling Price (also Business P&L, Lot P&L,
Final QC)."""
import pathlib
import re
import subprocess
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace as N

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_breakdown_rules():
    from services.repair_cost import RepairRates, APPLY_TAGS, APPLY_ASIS, APPLY_BOTH
    rr = RepairRates(labour={"laptop": (Decimal(700), Decimal(1200)), "server": (Decimal(1200), Decimal(0))},
                     packing=Decimal(100), packing_apply=APPLY_TAGS,
                     freight=Decimal(50), freight_apply=APPLY_BOTH)
    tag = N(device_type="Laptop", sub_category=None, sub_lot_number=None)
    asis = N(device_type="Laptop", sub_category=None, sub_lot_number="SL-1")
    srv = N(device_type="", sub_category="Server", sub_lot_number=None)
    assert rr.cost(tag, worked=True, had_l34=False) == 850          # 700 + 100 + 50
    assert rr.cost(tag, worked=True, had_l34=True) == 1350          # 1200 + 100 + 50
    assert rr.cost(tag, worked=False, had_l34=False) == 150         # not worked: no labour
    assert rr.cost(asis, worked=True, had_l34=False) == 750         # packing only on Tags
    assert rr.cost(asis, worked=True, had_l34=False, labour_ok=False) == 50
    assert rr.cost(srv, worked=True, had_l34=True) == 150           # With L3/L4 = 0 stays 0
    assert rr.cost(N(device_type="Tiny", sub_category=None, sub_lot_number=None), True, False) == 150
    rr2 = RepairRates(packing=Decimal(100), packing_apply=APPLY_ASIS)
    assert rr2.packing_for(asis) == 100 and rr2.packing_for(tag) == 0
    from services.repair_cost import cat_slug
    assert cat_slug("Mobile Phone") == "mobile" and cat_slug("TFT Monitor") == "tft_monitor"


def _cfg_snapshot():
    return _run(f"""
import asyncio, sys, json
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print(json.dumps({{k: str(v) for k, v in (await db.execute(text("select key, value from cost_config"))).all()}}))
asyncio.run(main())
""")


def _cfg_restore(snap):
    _run(f"""
import asyncio, sys, json
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
snap = json.loads({snap!r})
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from cost_config"))
        for k, v in snap.items():
            await db.execute(text("insert into cost_config (id, key, value) values (gen_random_uuid(), :k, :v)"),
                             {{"k": k, "v": v}})
        await db.commit()
asyncio.run(main())
""")


def test_rates_page_and_ready_min_price(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    plain, l34, idle = f"ITRC{sfx}P", f"ITRC{sfx}L", f"ITRC{sfx}I"
    snap = _cfg_snapshot()
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, StageMovement
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for bc, stages in [("{plain}", [DeviceStage.l1]), ("{l34}", [DeviceStage.l1, DeviceStage.l3]), ("{idle}", [])]:
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                       device_type="Laptop", device_price=10000, entity="ITRC{sfx}")
            db.add(d); await db.flush()
            for st in stages:
                db.add(StageMovement(device_id=d.id, from_stage=DeviceStage.stock_in, to_stage=st, moved_by="t"))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        page = app_client.get("/admin/cost-config").text
        for s in ("Packing Charges", "Freight Charges", "Labor Rates", 'name="labour_wo_laptop"',
                  'name="labour_w_laptop"', 'name="packing_apply"', 'name="freight_apply"', "On As-Is Lot"):
            assert s in page, s
        assert "Labour Rate per Repair Attempt" not in page
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        form = {"csrf_token": csrf, "packing_rate": "100", "packing_apply": "1",
                "freight_rate": "50", "freight_apply": "3", "gst_rate_intra": "18", "gst_rate_inter": "18",
                "default_paid_repair": "1500", "margin_item_type": "amount", "margin_item_value": "0",
                "margin_asis_type": "amount", "margin_asis_value": "0",
                "labour_wo_laptop": "700", "labour_w_laptop": "1200"}
        r = app_client.post("/admin/cost-config", data=form, headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert r.status_code == 302
        assert 'value="1200.00"' in app_client.get("/admin/cost-config").text

        j = app_client.get(f"/sales/ready/data?draw=1&start=0&length=25&entity=ITRC{sfx}").json()
        rows = {re.search(r"ITRC\w+", " ".join(c)).group(0): " ".join(c) for c in j["data"]}
        assert "10,850" in rows[plain]      # 10000 + 700 + 100 + 50
        assert "11,350" in rows[l34]        # 10000 + 1200 + 100 + 50
        assert "10,150" in rows[idle]       # not worked: packing + freight only
    finally:
        _cfg_restore(snap)
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from stage_movements where device_id in (select id from devices where barcode like 'ITRC{sfx}%')"))
        await db.execute(text("delete from devices where barcode like 'ITRC{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
