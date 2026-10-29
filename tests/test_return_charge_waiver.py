"""Paid Repair charge / waiver (2026-10-29, approved dbdiagram change): the
same Payment Due / Waive Off choice (optional waiver reason) on Customer
Return, External Tag return and Rental Return; Payment Due returns can be
Mark Paid with an Invoice / UTR number."""
import pathlib
import subprocess
import sys
import uuid
from decimal import Decimal

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def _ret(tag):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Return
async def main():
    async with AsyncSessionLocal() as db:
        r = (await db.execute(select(Return).join(Device, Return.device_id == Device.id)
             .where(Device.barcode == "{tag}"))).scalar_one()
        print("|".join(str(x) for x in (r.id, r.refund_amount, r.charge_decision, r.waiver_reason,
                                          r.payment_reference, r.paid_at is not None)))
asyncio.run(main())
""").split("|")


def _cleanup(tags):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for t in {list(tags)!r}:
            ids = f"(select id from devices where barcode = '{{t}}')"
            await db.execute(text(f"delete from returns where device_id in {{ids}}"))
            await db.execute(text(f"delete from stage_movements where device_id in {{ids}}"))
            await db.execute(text(f"delete from device_location_logs where device_id in {{ids}}"))
            await db.execute(text(f"delete from sales where device_id in {{ids}}"))
            await db.execute(text(f"delete from devices where barcode = '{{t}}'"))
        await db.commit()
asyncio.run(main())
""")


def test_charge_fields_rules():
    from routers.sales import _charge_fields
    assert _charge_fields(None, "waive_off", "x") == {"charge_decision": None, "waiver_reason": None}
    assert _charge_fields(Decimal("0"), "waive_off", "x") == {"charge_decision": None, "waiver_reason": None}
    assert _charge_fields(Decimal("1500"), "", "x") == {"charge_decision": "payment_due", "waiver_reason": None}
    assert _charge_fields(Decimal("1500"), "waive_off", "  ") == {"charge_decision": "waive_off", "waiver_reason": None}
    assert _charge_fields(Decimal("1500"), "waive_off", " goodwill ") == {"charge_decision": "waive_off",
                                                                           "waiver_reason": "goodwill"}


def test_external_return_waive_and_mark_paid(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    waived, due, free = f"ITWV{sfx}A", f"ITWV{sfx}B", f"ITWV{sfx}C"
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        form = app_client.get("/returns/new").text
        assert form.count('name="charge_decision"') >= 4 and 'name="waiver_reason"' in form
        assert 'id="rrWaiverReasonWrap"' in form            # Rental Return: same optional reason
        # Return Received By (2026-10-29): the signed-in user, shown disabled
        assert 'name="return_received_by" value="IQC Test User"' in form

        def submit(tag, amount, **extra):
            r = app_client.post("/returns/new/external", data={
                "csrf_token": csrf, "entity": "OxyPC Computers", "customer_name": "IT Waiver",
                "reason": "Not working", "condition_on_return": "Minor damage", "tag_number": tag,
                "quantity": "1", "paid_repair": amount, **extra,
            }, headers={"X-CSRF-Token": csrf}, follow_redirects=False)
            assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")

        submit(waived, "1500", charge_decision="waive_off", waiver_reason="Goodwill — repeat dealer")
        submit(due, "1500", charge_decision="payment_due", waiver_reason="ignored when due")
        submit(free, "0", charge_decision="waive_off")
        w, d, f = _ret(waived), _ret(due), _ret(free)
        assert w[1:6] == ["1500.00", "waive_off", "Goodwill — repeat dealer", "None", "False"], w
        assert d[1:6] == ["1500.00", "payment_due", "None", "None", "False"], d
        assert f[2] == "None", f                     # nothing to charge -> no decision

        html = app_client.get("/returns").text
        assert "Waived Off" in html and "Goodwill — repeat dealer" in html
        assert "Payment Due" in html and 'id="returnPaidModal"' in html
        rcpt = app_client.get(f"/returns/{w[0]}/receipt").text
        assert "Waived Off — Goodwill — repeat dealer" in rcpt

        # Mark Paid needs a reference, then records it
        r = app_client.post(f"/returns/{d[0]}/mark-paid", data={"csrf_token": csrf, "payment_reference": ""},
                            headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert "error" in r.headers["location"]
        r = app_client.post(f"/returns/{d[0]}/mark-paid", data={"csrf_token": csrf, "payment_reference": "UTR998877"},
                            headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert "success" in r.headers["location"]
        d = _ret(due)
        assert d[4:6] == ["UTR998877", "True"], d
        assert "Paid (UTR998877" in app_client.get(f"/returns/{d[0]}/receipt").text
        # a waived return can't be marked paid
        r = app_client.post(f"/returns/{w[0]}/mark-paid", data={"csrf_token": csrf, "payment_reference": "X"},
                            headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert "error" in r.headers["location"]
    finally:
        _cleanup([waived, due, free])
