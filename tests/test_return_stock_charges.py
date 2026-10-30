"""Return Stock charges (2026-10-30): a return with a Paid Repair Cost that
is not waived is payment due — Repair Cost = that amount, Labour Cost =
Additional Rates labour (Without L3/L4 after completing L1/L2, With L3/L4
after passing L3/L4 — only movements after the returned sale), Part Cost =
parts handed over since that sale. Verify Payment (needs a Payment Receipt
Number) must come before Ready to Dispatch."""
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


def _seed(bc, l34):
    return _run(f"""
import asyncio, sys
from datetime import timedelta
from decimal import Decimal
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement
from models.sales import Sale, Return
from models.part_request import PartRequest
from models.spare_parts import SparePart
from services.repair_cost import load_repair_rates
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        now = app_now()
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{bc}", lot_id=lot.id, brand="Dell", model="Latitude 7490", device_type="Laptop",
                     current_stage=DeviceStage.ready_to_sale, return_status=True, device_price=15000)
        db.add(dev); await db.flush()
        # old repair history BEFORE the sale: L3/L4 pass + a part — must be ignored
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.l3, to_stage=DeviceStage.l1,
                             moved_at=now - timedelta(days=30), moved_by="t"))
        sp = SparePart(name="ITRSC Keyboard {bc}", part_code="P{bc[-10:]}", category="Keyboard", qty_in_stock=5, unit_price=Decimal("450"))
        db.add(sp); await db.flush()
        db.add(PartRequest(device_id=dev.id, barcode="{bc}", part_id=sp.id, part_name=sp.name, status="received",
                           qty_requested=1, qty_handed_over=1, created_at=now - timedelta(days=29)))
        sale = Sale(sale_number="SALEIT{bc[4:]}", device_id=dev.id, sale_price=20000, customer_name="Cust RS",
                    customer_phone="9998887777", customer_state="Delhi", payment_mode="Cash",
                    sold_at=now - timedelta(days=20))
        db.add(sale); await db.flush()
        db.add(Return(sale_id=sale.id, device_id=dev.id, reason="Not working", return_date=now - timedelta(days=5),
                      refund_amount=Decimal("1500"), charge_decision="payment_due",
                      approval_status="pending", warranty_status="out_of_warranty"))
        # repair after the return: L1/L2 completed (and L3/L4 passed when l34)
        if {l34!r}:
            db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.l3, to_stage=DeviceStage.l1,
                                 moved_at=now - timedelta(days=3), moved_by="t"))
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.l1, to_stage=DeviceStage.qc_check,
                             moved_at=now - timedelta(days=2), moved_by="t"))
        db.add(PartRequest(device_id=dev.id, barcode="{bc}", part_id=sp.id, part_name=sp.name, status="handed_over",
                           qty_requested=2, qty_handed_over=2, created_at=now - timedelta(days=2)))
        await db.commit()
        r = await load_repair_rates(db)
        print(r.labour_for(dev, {l34!r}))
asyncio.run(main())
""")


def _cleanup(bcs):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in {list(bcs)!r}:
            ids = f"(select id from devices where barcode = '{{bc}}')"
            for t in ("returns", "part_requests", "stage_movements", "sales", "device_location_logs"):
                await db.execute(text(f"delete from {{t}} where device_id in {{ids}}"))
            await db.execute(text(f"delete from devices where barcode = '{{bc}}'"))
            await db.execute(text(f"delete from spare_parts where name = 'ITRSC Keyboard {{bc}}'"))
        await db.commit()
asyncio.run(main())
""")


def test_return_stock_charges_and_verify_then_dispatch(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    a, b = f"ITRSCA{sfx}", f"ITRSCB{sfx}"
    try:
        lab_wo = _seed(a, False)
        lab_w = _seed(b, True)
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        html = app_client.get("/stock", follow_redirects=True).text

        def row(bc):
            return html.split(f'value="{bc}"', 1)[1].split("</tr>", 1)[0]

        ra, rb = row(a), row(b)
        # Repair 1500 | Labour Without / With L3/L4 | Part 2 x 450 (the pre-sale part ignored)
        assert ">1500.00<" in ra and f">{float(lab_wo):.2f}<" in ra and ">900.00<" in ra, ra
        assert f">{float(lab_w):.2f}<" in rb and 'title="With L3/L4"' in rb
        assert 'title="Without L3/L4"' in ra
        assert "Verify Payment" in ra and 'data-part-cost="900' in ra
        assert "rs-complete-btn d-none" in ra                      # Ready to Dispatch hidden until paid
        # Current Stage: outline stage badge + filled status (2026-10-30)
        assert "border-secondary text-secondary" in ra and ">Payment Pending<" in ra
        assert 'data-rs-tab="received"' in html.split(f'value="{a}"', 1)[0].rsplit("<tr", 1)[1]

        # Ready to Dispatch refused before Verify Payment; receipt number required
        r = app_client.post(f"/stock/return-stock/{a}/complete", data={"csrf_token": csrf})
        assert r.status_code == 400 and "Verify Payment" in r.json()["detail"]
        r = app_client.post(f"/stock/return-stock/{a}/verify",
                            data={"csrf_token": csrf, "repair_cost": "1500", "labour_cost": "700"})
        assert r.status_code == 400
        r = app_client.post(f"/stock/return-stock/{a}/verify", data={
            "csrf_token": csrf, "repair_cost": "1500", "labour_cost": "650", "payment_reference": "RCPT-77"})
        assert r.status_code == 200 and r.json()["paid"] is True, r.text
        ra = app_client.get("/stock", follow_redirects=True).text.split(f'value="{a}"', 1)[1].split("</tr>", 1)[0]
        assert "rs-verify-btn" not in ra and "Ready to Dispatch" in ra and "rs-complete-btn d-none" not in ra
        assert ">650.00<" in ra and ">Dispatch Pending<" in ra      # verified Labour kept
        r = app_client.post(f"/stock/return-stock/{a}/complete", data={"csrf_token": csrf})
        assert r.status_code == 200 and r.json()["ok"], r.text
        r = app_client.post(f"/stock/return-stock/{a}/complete", data={"csrf_token": csrf})
        assert r.status_code == 400                                 # no second re-sale

        def tr(bc):
            h = app_client.get("/stock", follow_redirects=True).text
            i = h.index(f'href="/devices/{bc}"')
            return h[h.rindex("<tr", 0, i):h.index("</tr>", i)]
        t = tr(a)                                                   # still listed until Gate Pass exit
        assert 'data-rs-tab="received"' in t and ">Dispatch Pending<" in t and "At Gate Pass" in t
        assert "rs-row-check" not in t
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("update sales set delivery_status = 'dispatched' where notes like 'Re-sale after customer return%' "
                              "and device_id = (select id from devices where barcode = '{a}')"))
        await db.commit()
asyncio.run(main())
""")
        t = tr(a)
        assert 'data-rs-tab="completed"' in t and ">Return Complete<" in t and "Exited" in t
    finally:
        _cleanup([a, b])
