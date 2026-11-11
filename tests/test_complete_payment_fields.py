"""Complete Payment fields (2026-11-09, Pankaj): Supplier Payments (PO) and
Customer Receipts (Quote) ask for Payment Date, Payment Amount, Payment
UTR/Invoice Number and Payment Mode, and save them as a Supplier Payment /
Customer Receipt record."""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.test_return_charge_waiver import _run, ROOT


def _sql(stmt):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text({stmt!r}))
        try:
            print("|".join(str(x) for x in r.first() or []))
        except Exception:
            print("")
        await db.commit()
asyncio.run(main())
""")


def test_complete_payment_saves_date_amount_mode(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    po_id, q_id = uuid.uuid4(), uuid.uuid4()
    po_no, q_no = f"PO-IT-{sfx}", f"QT-IT-{sfx}"
    try:
        _sql(f"insert into crm_purchase_orders (id, po_number, po_date, total_amount, status, payment_status) "
             f"values ('{po_id}', '{po_no}', current_date, 50000, 'issued', 'pending')")
        _sql(f"insert into crm_quotes (id, quote_number, quote_date, total_amount, status, payment_status) "
             f"values ('{q_id}', '{q_no}', current_date, 25000, 'accepted', 'pending')")
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        h = {"X-CSRF-Token": csrf}

        # Missing mode -> refused, nothing saved
        r = app_client.post(f"/accounts/po/{po_id}/complete-payment", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "payment_date": "2026-11-09", "payment_amount": "50000",
                                  "utr_number": f"UTR{sfx}", "payment_mode": ""})
        assert "error=" in r.headers["location"]
        assert _sql(f"select payment_status from crm_purchase_orders where id='{po_id}'") == "pending"

        r = app_client.post(f"/accounts/po/{po_id}/complete-payment", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "payment_date": "2026-11-08", "payment_amount": "49500.50",
                                  "utr_number": f"UTR{sfx}", "payment_mode": "neft"})
        assert "success=" in r.headers["location"], r.headers["location"]
        row = _sql(f"select payment_date, amount, payment_mode, reference_no from supplier_payments where po_id='{po_id}'")
        assert row == f"2026-11-08|49500.50|neft|UTR{sfx}", row
        assert _sql(f"select payment_status from crm_purchase_orders where id='{po_id}'") == "paid"

        r = app_client.post(f"/accounts/quote/{q_id}/complete-payment", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "payment_date": "2026-11-09", "payment_amount": "25000",
                                  "utr_number": f"INV{sfx}", "payment_mode": "upi"})
        assert "success=" in r.headers["location"], r.headers["location"]
        row = _sql(f"select receipt_date, amount, payment_mode, reference_no from customer_receipts where reference_no='INV{sfx}'")
        assert row == f"2026-11-09|25000.00|upi|INV{sfx}", row
    finally:
        _sql(f"delete from supplier_payments where po_id='{po_id}'")
        _sql(f"delete from customer_receipts where reference_no='INV{sfx}'")
        _sql(f"delete from crm_purchase_orders where id='{po_id}'")
        _sql(f"delete from crm_quotes where id='{q_id}'")
