"""Return approval / rejection stages (2026-11-09, Pankaj).

* Approve: the tag stays at Stock In (it used to move to IQC, which disabled
  Return Stock's Move to L1/L2 and Only Stock In).
* Reject: the tag goes back to Sold, leaves Return Stock (return flag off,
  "Return Rejected"); refused once repair has started (tag left Stock In).
"""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.test_return_charge_waiver import _run, _cleanup, ROOT


def _state(tag):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.sales import Return
async def main():
    async with AsyncSessionLocal() as db:
        d = (await db.execute(select(Device).where(Device.barcode == "{tag}"))).scalar_one()
        r = (await db.execute(select(Return).where(Return.device_id == d.id))).scalars().first()
        print("|".join(str(x) for x in (d.current_stage.value, d.return_status, d.tag_return_status,
                                          r.approval_status, r.id)))
asyncio.run(main())
""").split("|")


def _set_stage(tag, stage):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("update devices set current_stage = '{stage}' where barcode = '{tag}'"))
        await db.commit()
asyncio.run(main())
""")


def test_approve_keeps_stock_in_and_reject_goes_back_to_sold(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    ok_tag, rej_tag, late_tag = f"ITAR{sfx}A", f"ITAR{sfx}B", f"ITAR{sfx}C"
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        h = {"X-CSRF-Token": csrf}
        for tag in (ok_tag, rej_tag, late_tag):
            r = app_client.post("/returns/new/external", data={
                "csrf_token": csrf, "entity": "OxyPC Computers", "customer_name": "IT Return Stage",
                "reason": "Not working", "condition_on_return": "Minor damage", "complaint_text": "Test",
                "tag_number": tag, "quantity": "1", "paid_repair": "0", "charge_decision": "waive_off",
                "cpu": "0", "generation": "0", "ram": "0", "storage": "0"}, headers=h, follow_redirects=False)
            assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")

        # Approve -> stays at Stock In (not IQC)
        st = _state(ok_tag)
        _set_stage(ok_tag, "stock_in")      # the internal-return path puts it here on submit
        r = app_client.post(f"/returns/{st[4]}/approve", data={"csrf_token": csrf}, headers=h, follow_redirects=False)
        assert "success" in r.headers["location"]
        st = _state(ok_tag)
        assert st[0] == "stock_in" and st[3] == "approved", st

        # Reject -> back to Sold, off Return Stock
        st = _state(rej_tag)
        _set_stage(rej_tag, "stock_in")
        r = app_client.post(f"/returns/{st[4]}/reject", data={"csrf_token": csrf, "rejection_reason": "No fault found"},
                            headers=h, follow_redirects=False)
        assert "success" in r.headers["location"]
        st = _state(rej_tag)
        assert st[0] == "sold" and st[1] == "False" and st[2] == "Return Rejected" and st[3] == "rejected", st

        # Repair already started -> reject refused, tag untouched
        st = _state(late_tag)
        _set_stage(late_tag, "l1")
        r = app_client.post(f"/returns/{st[4]}/reject", data={"csrf_token": csrf, "rejection_reason": "x"},
                            headers=h, follow_redirects=False)
        assert "error" in r.headers["location"]
        st = _state(late_tag)
        assert st[0] == "l1" and st[3] == "pending", st
    finally:
        _cleanup([ok_tag, rej_tag, late_tag])
