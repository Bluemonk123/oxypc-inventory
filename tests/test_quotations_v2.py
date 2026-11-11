"""Quotations v2 (2026-11-10, Pankaj).

* Both kinds: Added -> Negotiation -> Approved / Rejected / Expired.
* Numbers: Deal Quotes QT-DEAL-<MMYYYY><NNNN>, Telecalling QT-TELE-<MMYYYY><NNNN>.
* Telecalling Level 1 approval only by the creator's Report To hierarchy (or
  admin); the Quote download works only once Approved.
* From Entity is saved on a new Deal Quote.
"""
import re
import uuid
from datetime import date

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.test_return_charge_waiver import _run, ROOT


def _sql(stmt, params=None):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text({stmt!r}), {params or {}!r})
        try:
            row = r.first()
            print("|".join(str(x) for x in row) if row else "")
        except Exception:
            print("")
        await db.commit()
asyncio.run(main())
""")


def _mm():
    d = date.today()
    return f"{d.month:02d}{d.year}"


def test_deal_quote_number_status_and_entity(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    h = {"X-CSRF-Token": csrf}
    tag = uuid.uuid4().hex[:6]
    r = app_client.post("/crm/quotes/new", headers=h, follow_redirects=False, data={
        "csrf_token": csrf, "quote_kind": "buyer", "from_entity": "Renew Circuits",
        "payment_terms": f"IT-{tag}", "device_type": ["Laptop"], "grade": ["A"], "po_category": [""],
        "quantity": ["2"], "unit_price": ["100"]})
    assert r.status_code in (302, 303), r.text[:300]
    row = _sql("select id, quote_number, status, from_entity from crm_quotes where payment_terms = :t", {"t": f"IT-{tag}"})
    qid, num, status, ent = row.split("|")
    try:
        assert re.fullmatch(rf"QT-DEAL-{_mm()}\d{{4}}", num), num
        assert status == "added" and ent == "Renew Circuits"
        # old status value refused, new one accepted
        r = app_client.post(f"/crm/quotes/{qid}/status", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "new_status": "accepted"})
        assert "error=" in r.headers["location"]
        r = app_client.post(f"/crm/quotes/{qid}/status", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "new_status": "negotiation"})
        assert _sql("select status from crm_quotes where id = :i", {"i": qid}) == "negotiation"
    finally:
        _sql("delete from crm_quote_items where quote_id = :i", {"i": qid})
        _sql("delete from crm_quotes where id = :i", {"i": qid})


def test_telecalling_level1_by_report_to_chain(app_client, make_user):  # noqa: F811
    creator, _ = make_user("telecaller")
    manager, mpw = make_user("sales_manager")
    boss, bpw = make_user("sales_manager")
    outsider, opw = make_user("sales_manager")
    _sql("update users set reports_to = :m where username = :u", {"m": manager, "u": creator})
    _sql("update users set reports_to = :b where username = :u", {"b": boss, "u": manager})
    tag = uuid.uuid4().hex[:6]
    did, qid = str(uuid.uuid4()), str(uuid.uuid4())
    num = f"QT-TELE-{_mm()}9{tag[:3].upper()}"[:18]
    _sql("insert into dealers (id, dealer_code, business_name, portal_enabled, price_segment, portal_token_only, "
         "portal_password_version) values (:i, :c, 'IT Dealer', false, 'new_dealer', false, 1)",
         {"i": did, "c": f"ITD{tag}"})
    _sql("insert into dealer_quotations (id, quote_number, dealer_id, customer_name, total_quantity, total_price, "
         "status, created_by, created_at) values (:i, :n, :d, 'IT Dealer', 1, 100, 'added', :c, now())",
         {"i": qid, "n": num, "d": did, "c": creator})
    try:
        # outsider (not in the chain) is refused
        _login(app_client, outsider, opw)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post(f"/dealer-quotations/{qid}/decision", headers={"X-CSRF-Token": csrf}, follow_redirects=False,
                            data={"csrf_token": csrf, "decision": "approve"})
        assert "error=" in r.headers["location"]
        assert _sql("select status from dealer_quotations where id = :i", {"i": qid}) == "added"
        # download refused before approval
        r = app_client.get(f"/dealer-quotations/{qid}/print", follow_redirects=False)
        assert r.status_code in (302, 303, 403)
        # the creator's manager's manager (two levels up) can approve
        _login(app_client, boss, bpw)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        page = app_client.get("/telesales-dashboard")
        assert num in page.text      # listed under Pending Approval
        r = app_client.post(f"/dealer-quotations/{qid}/decision", headers={"X-CSRF-Token": csrf}, follow_redirects=False,
                            data={"csrf_token": csrf, "decision": "approve"})
        assert "success=" in r.headers["location"], r.headers["location"]
        assert _sql("select status, confirmed_by from dealer_quotations where id = :i", {"i": qid}) == f"approved|{boss}"
    finally:
        _sql("delete from dealer_quotation_items where quotation_id = :i", {"i": qid})
        _sql("delete from dealer_quotations where id = :i", {"i": qid})
        _sql("delete from dealers where id = :i", {"i": did})
        _sql("update users set reports_to = null where username in (:a, :b)", {"a": creator, "b": manager})
