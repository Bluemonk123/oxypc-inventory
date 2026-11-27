"""RMA Devices (2026-11-27, Pankaj): a page above Pre-Sale QC built on the
L1/L2 queue. Return Stock -> Move to RMA (was Move to L1/L2); Pick This /
Start Repair / part requests as on L1/L2; Mark Complete is "Ready" (customer
return -> Sold, Return Stock Dispatch Pending); Replace gives the customer any
tag in stock: the customer tag goes to IQC ("Replaced by <tag>", off both
tables), the replacement tag comes onto RMA Devices as Ready ("Replaced with
<tag>") and Return Stock lists it with the customer tag's return. Permission
module rma_devices is off by default. All Inventory shows RMA / RR / RS next to
the entity; Product IQC shows "Customer Return" under the tag."""
import pathlib
import re
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_rma_stage_permission_and_sidebar_registered():
    from models.device import DeviceStage, STAGE_LABELS, DROPDOWN_STAGES
    from models.role_permissions import DEFAULT_DISABLED_MODULES
    from routers.master import PERM_MODULES
    from services.stage_work_orders import _SPEC
    from auth.route_actions import ROUTE_ACTIONS
    assert DeviceStage.rma.value == "rma" and STAGE_LABELS[DeviceStage.rma] == "RMA Devices"
    assert DeviceStage.rma not in DROPDOWN_STAGES
    assert _SPEC[DeviceStage.rma][:2] == ("rma", "RMA-")
    assert "rma_devices" in DEFAULT_DISABLED_MODULES
    keys = [k for k, _ in PERM_MODULES]
    assert keys.index("rma_devices") == keys.index("pre_sale_iqc") - 1
    for path in ("/repair/rma/pick", "/repair/rma/start", "/repair/rma/ready", "/repair/rma/replace",
                 "/repair/rma/bulk-part-request"):
        assert ROUTE_ACTIONS[("POST", path)][0] == "rma_devices"
    assert ROUTE_ACTIONS[("POST", "/stock/return-stock/move-rma")] == ("stock", "edit")
    base = (ROOT / "templates" / "base.html").read_text()
    assert base.index('data-sb="/repair/rma"') < base.index('data-sb="/pre-sale-iqc"')
    stock = (ROOT / "templates" / "lots" / "stock_in.html").read_text()
    assert "/stock/return-stock/move-rma" in stock and "Move to RMA (" in stock


def test_rma_ready_and_replace_flow(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    a, b, y = f"ITRMA{sfx}A", f"ITRMA{sfx}B", f"ITRMA{sfx}Y"
    ids = _run(f"""
import asyncio, sys, uuid
from datetime import timedelta
sys.path.insert(0, r"{ROOT}")
from decimal import Decimal
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Sale, Return
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).where(Lot.is_trashed.isnot(True)).limit(1))).scalars().first()
        out = []
        for bc in ("{a}", "{b}"):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot.id, brand="Dell", model="Latitude 7490",
                       current_stage=DeviceStage.stock_in, return_status=True, tag_return_status="Return for Repair",
                       is_active=True)
            db.add(d); await db.flush()
            s = Sale(sale_number="T" + uuid.uuid4().hex[:12].upper(), device_id=d.id, sale_price=Decimal("20000"),
                     customer_name="RMA Test Customer", sold_at=app_now() - timedelta(days=5), invoice_no="INV-" + bc)
            db.add(s); await db.flush()
            db.add(Return(sale_id=s.id, device_id=d.id, reason="Not working", condition_on_return="As sold",
                          processed_by="tester", approval_status="approved", return_date=app_now() - timedelta(days=1)))
            out.append(str(d.id))
        r = Device(id=uuid.uuid4(), barcode="{y}", lot_id=lot.id, brand="HP", model="EliteBook 840",
                   current_stage=DeviceStage.ready_to_sale, is_active=True)
        db.add(r)
        await db.commit()
        print(" ".join(out))
asyncio.run(main())
""").split()
    ida, idb = ids
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        H = {"X-CSRF-Token": csrf}

        def post(url, **data):
            return app_client.post(url, data={"csrf_token": csrf, **data}, headers=H, follow_redirects=False)

        def rs_status(bc):
            h = app_client.get("/stock").text
            t = h[h.index('id="returnStockTable"'):]
            m = re.search(r'<tr data-rs-search="' + re.escape(bc) + r' .*?</tr>', t, re.S)
            if not m:
                return None
            return re.search(r'rs-status [^"]*">([^<]+)<', m.group(0)).group(1), m.group(0)

        r = post("/stock/return-stock/move-rma", barcodes=f"{a},{b}")
        assert r.json()["moved"] == 2, r.text
        page = app_client.get("/repair/rma").text
        assert "Devices in RMA" in page and a in page and b in page and "request-l3l4-btn\"" not in page
        for did in (ida, idb):
            assert post("/repair/rma/pick", device_id=did).status_code == 302
            assert post("/repair/rma/start", device_id=did).status_code == 302
        page = app_client.get("/repair/rma").text
        row = re.search(r'<tr data-device-id="%s".*?</tr>' % ida, page, re.S).group(0)
        assert "/repair/rma/ready" in row and "rma-replace-btn" in row
        assert rs_status(a)[0] == "Repair Pending"

        # Ready = Mark Complete: customer return -> Sold, Return Stock Dispatch Pending.
        r = post("/repair/rma/ready", device_id=ida)
        assert r.status_code == 302 and "error" not in r.headers["location"]
        assert rs_status(a)[0] == "Dispatch Pending"

        # Replace: B -> IQC (off both tables), Y -> RMA as Ready, in Return Stock.
        info = app_client.get("/repair/rma/return-info", params={"device_id": idb}).json()
        assert info["reason"] == "Not working" and info["return_by"] == "tester"
        found = app_client.get("/repair/rma/replace-search", params={"q": y}).json()["items"]
        assert [i["barcode"] for i in found] == [y]
        assert app_client.get("/repair/rma/replace-search", params={"q": a}).json()["items"] == []   # sold: not stock
        r = post("/repair/rma/replace", device_id=idb, replacement_barcode=y)
        assert r.status_code == 302 and "success" in r.headers["location"], r.headers.get("location")
        page = app_client.get("/repair/rma").text
        assert f'data-search="{b} ' not in page
        assert f"Replaced with {b}" in page
        assert rs_status(b) is None
        st, row = rs_status(y)
        assert st == "Dispatch Pending" and f"Replaced with {b}" in row

        # Badges (2026-11-27): All Inventory RR next to the entity; IQC "Customer Return".
        cell = app_client.get("/devices/data", params={"draw": 1, "start": 0, "length": 5,
                                                        "search[value]": b}).json()["data"][0][1]
        assert f'title="Replaced with {y}">RR<' in cell
        cell = app_client.get("/iqc/data", params={"draw": 1, "start": 0, "length": 5,
                                                    "search[value]": b}).json()["data"][0][2]
        assert f'title="Replaced with {y}">Customer Return<' in cell

        # Ready to Dispatch on the replacement: ₹0 re-sale to the customer.
        r = post(f"/stock/return-stock/{y}/complete")
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert rs_status(y)[0] == "Ready at Gate Pass"
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select current_stage||'|'||coalesce(tag_return_status,'') from devices where barcode='{b}'"))).scalar())
        print((await db.execute(text("select d.current_stage||'|'||s.sale_price::text||'|'||s.customer_name||'|'||d.invoice_number "
              "from devices d join sales s on s.device_id=d.id where d.barcode='{y}' order by s.sold_at desc limit 1"))).scalar())
asyncio.run(main())
""").splitlines()
        assert out[0] == f"iqc|Replaced by {y}"
        assert out[1] == f"sold|0.00|RMA Test Customer|INV-{b}"
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in ('{a}','{b}','{y}')"
        sales = f"select id from sales where device_id in ({{ids}})"
        fk = "select c.conrelid::regclass::text, a.attname from pg_constraint c join pg_attribute a " \
             "on a.attrelid = c.conrelid and a.attnum = c.conkey[1] where c.contype = 'f' and c.confrelid = '{{}}'::regclass"
        for t, col in (await db.execute(text(fk.format("sales")))).all():
            if t != "sales":
                await db.execute(text(f"delete from {{t}} where {{col}} in ({{sales}})"))
        for t, col in (await db.execute(text(fk.format("devices")))).all():
            if t != "devices":
                await db.execute(text(f"delete from {{t}} where {{col}} in ({{ids}})"))
        await db.execute(text(f"delete from devices where id in ({{ids}})"))
        await db.commit()
asyncio.run(main())
""")


def test_return_badge_html_and_rules():
    from types import SimpleNamespace as NS
    import asyncio
    from utils.return_badges import return_badge_map, return_badge_html
    rr = NS(id="1", tag_return_status="Replaced by TAGC", replaced=None)
    assert asyncio.run(return_badge_map(None, [rr])) == {"1": ("RR", "Replaced with TAGC")}
    html = return_badge_html({"1": ("RS", "Sold and Replaced for TAGB")}, "1")
    assert ">RS<" in html and 'title="Sold and Replaced for TAGB"' in html
    assert return_badge_html({}, "1") == ""
