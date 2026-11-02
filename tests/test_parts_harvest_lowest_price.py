"""Price rule (2026-11-02): New rows = latest Parts GRN unit price, Harvest
rows = lowest Parts GRN unit price, of the same Part Name + Make + Model."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def test_new_latest_harvest_lowest():
    nm = "ITPrice" + uuid.uuid4().hex[:6]
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, sys, uuid
from datetime import timedelta
from decimal import Decimal
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.parts_grn import PartsGRN, PartsGRNLineItem
from models.spare_parts import SparePart
from services.parts_stock import apply_group_price, lowest_grn_price, latest_grn_price, price_for
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        g = PartsGRN(id=uuid.uuid4(), grn_number="T" + uuid.uuid4().hex[:8])
        db.add(g); await db.flush()
        now = app_now()
        for price, age in ((Decimal("900"), 3), (Decimal("700"), 2), (Decimal("800"), 1)):
            db.add(PartsGRNLineItem(grn_id=g.id, part_id="X", part_name="{nm}", part_brand="Dell", part_model="E1",
                                    price=price, is_harvest=False, created_at=now - timedelta(days=age)))
        new = SparePart(part_code="PN" + uuid.uuid4().hex[:6], name="{nm}", make="Dell", model="E1", source="new", category="Other",
                        unit_price=1, qty_in_stock=1, crate_number="C1")
        hv = SparePart(part_code="PH" + uuid.uuid4().hex[:6], name="{nm}", make="Dell", model="E1", source="harvest", category="Other",
                       unit_price=1, qty_in_stock=1, crate_number="C1")
        db.add_all([new, hv]); await db.flush()
        lat = await latest_grn_price(db, "{nm}", "Dell", "E1"); low = await lowest_grn_price(db, "{nm}", "Dell", "E1")
        await apply_group_price(db, "{nm}", "Dell", "E1", lat)
        print(lat, low, new.unit_price, hv.unit_price, await price_for(db, "{nm}", "Dell", "E1", "harvest"))
        await db.rollback()
asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    lat, low, newp, hvp, pf = r.stdout.split()
    assert (float(lat), float(low), float(newp), float(hvp), float(pf)) == (800, 700, 800, 700, 700), r.stdout


def test_upload_price_rules(app_client, make_user):  # noqa: F811
    """Upload New / Harvest (2026-11-02): a price in the file overwrites that
    row only; a blank price takes the formula (New = latest GRN, Harvest =
    lowest GRN); upload prices never count as GRN prices."""
    import json
    from tests.test_parts_crate_rows import _sql
    from tests.test_iqc_new_user import _login
    name = "ITUPL" + uuid.uuid4().hex[:6].upper()
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    H = {"X-CSRF-Token": csrf}
    try:
        items = [{"part_name": name, "part_brand": "Dell", "part_model": "E1", "crate_number": "U-1",
                  "physical_qty": "2", "price": "900"},
                 {"part_name": name, "part_brand": "Dell", "part_model": "E1", "crate_number": "U-1",
                  "physical_qty": "1", "price": "700"}]
        r = app_client.post("/parts-grn/new", data={"csrf_token": csrf, "vendor_name": "T",
                                                     "line_items_json": json.dumps(items)},
                            headers=H, follow_redirects=False)
        assert r.status_code == 303, r.text[:300]
        hdr = "part_name,part_brand,part_model,crate_number,physical_qty,price\n"

        def up(source, body):
            return app_client.post("/spare-parts/bulk-upload", data={"csrf_token": csrf, "source": source},
                                   files={"file": ("p.csv", (hdr + body).encode(), "text/csv")}, headers=H)
        # blank price -> formula: Harvest = lowest (700), New = latest (700, the last line)
        assert up("harvest", f"{name},Dell,E1,U-1,4,\n").status_code == 200
        assert up("new", f"{name},Dell,E1,U-2,3,\n").status_code == 200
        rows = dict(l.rsplit("|", 1) for l in _sql(
            f"select source || ':' || crate_number, unit_price from spare_parts where name = '{name}' "
            f"and is_trashed is not true").splitlines())
        assert rows["harvest:U-1"] == "700.00" and rows["new:U-2"] == "700.00", rows
        # price in file -> that row only; other rows untouched; not a GRN price
        assert up("new", f"{name},Dell,E1,U-2,3,50\n").status_code == 200
        rows = dict(l.rsplit("|", 1) for l in _sql(
            f"select source || ':' || crate_number, unit_price from spare_parts where name = '{name}' "
            f"and is_trashed is not true").splitlines())
        assert rows["new:U-2"] == "50.00" and rows["new:U-1"] == "700.00" and rows["harvest:U-1"] == "700.00", rows
        assert up("harvest", f"{name},Dell,E1,U-1,4,\n").status_code == 200   # lowest still 700, not 50
        assert _sql(f"select unit_price from spare_parts where name = '{name}' and source = 'harvest' "
                    f"and is_trashed is not true") == "700.00"
    finally:
        _sql(f"update spare_parts set is_trashed = true where name = '{name}'")
