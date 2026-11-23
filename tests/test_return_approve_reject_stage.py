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

        # No approval step (2026-11-21, Pankaj): every return is approved as
        # it is logged, and Approve / Reject no longer apply to it.
        for tag in (ok_tag, rej_tag, late_tag):
            st = _state(tag)
            assert st[3] == "approved", st
        st = _state(ok_tag)
        r = app_client.post(f"/returns/{st[4]}/approve", data={"csrf_token": csrf}, headers=h, follow_redirects=False)
        assert "already+approved" in r.headers["location"]
        r = app_client.post(f"/returns/{st[4]}/reject", data={"csrf_token": csrf, "rejection_reason": "x"},
                            headers=h, follow_redirects=False)
        assert "already+approved" in r.headers["location"]
    finally:
        _cleanup([ok_tag, rej_tag, late_tag])
