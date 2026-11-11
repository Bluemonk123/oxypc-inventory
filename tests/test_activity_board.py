"""Activity Board (2026-11-06, Pankaj): Supplier Deals (Inspection), Customer
Deals (Screening) and Account Verification (Verify Credit / Map Manager)."""
import io
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


def _q(sql):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text({sql!r}))
        try:
            print("|".join("" if v is None else str(v) for v in r.first() or []))
        except Exception:
            pass
        await db.commit()
asyncio.run(main())
""").splitlines()[-1:] or [""]


def test_activity_board_flows(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    admin, apw = make_user("admin")
    worker, wpw = make_user("sales")
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.crm import CRMContact, CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep
from models.crm_team import CRMContactTeamMember
from models.user import User
async def main():
    async with AsyncSessionLocal() as db:
        w = (await db.execute(select(User).where(User.username == "{worker}"))).scalar_one()
        c = CRMContact(contact_code="IT{sfx}", company_name="ITAB Co {sfx}", contact_type="both", credit_limit=50000)
        db.add(c); await db.flush()
        d = CRMSourcingDeal(deal_number="SD-IT{sfx}", title="AB supplier {sfx}", stage="inspection", contact_id=c.id, created_by="{admin}")
        o = CRMSalesOpportunity(opp_number="OPP-IT{sfx}", title="AB customer {sfx}", stage="screening", contact_id=c.id, created_by="{admin}")
        d2 = CRMSourcingDeal(deal_number="SD-IU{sfx}", title="AB other {sfx}", stage="inspection", contact_id=c.id)
        db.add_all([d, o, d2]); await db.flush()
        db.add(CRMSourcingStep(deal_id=d.id, stage="inspection", step_key="inspection_assign", assigned_to="{worker}", status="done"))
        db.add(CRMSalesStep(deal_id=o.id, stage="screening", step_key="screening_assign", assigned_to="{worker}", status="done"))
        db.add(CRMSourcingStep(deal_id=d2.id, stage="inspection", step_key="inspection_assign", assigned_to="{admin}", status="done"))
        db.add(CRMContactTeamMember(contact_id=c.id, user_id=w.id, role_bucket="am_dm_gm"))
        await db.commit()
asyncio.run(main())
""")
    try:
        # the assignee sees only their own deals
        _login(app_client, worker, wpw)
        csrf = app_client.cookies.get("csrf_token") or "x"
        h = {"X-CSRF-Token": csrf}
        html = app_client.get("/activity-board").text
        assert "SD-IT" + sfx in html and "OPP-IT" + sfx in html and "SD-IU" + sfx not in html
        assert "Inspection Pending" in html and "Screening Pending" in html
        did = _q(f"select id from crm_sourcing_deals where deal_number='SD-IT{sfx}'")[0]
        oid = _q(f"select id from crm_sales_opportunities where opp_number='OPP-IT{sfx}'")[0]
        cid = _q(f"select id from crm_contacts where contact_code='IT{sfx}'")[0]

        # upload before Verify is refused; Verify -> Started; upload -> Done; reupload replaces
        r = app_client.post(f"/activity-board/supplier/{did}/report", data={"csrf_token": csrf}, headers=h,
                            files={"file": ("r.pdf", io.BytesIO(b"%PDF-1.4 x"), "application/pdf")}, follow_redirects=False)
        assert "error" in r.headers["location"]
        app_client.post(f"/activity-board/supplier/{did}/start", data={"csrf_token": csrf}, headers=h)
        assert "Inspection Started" in app_client.get("/activity-board").text
        for _ in range(2):
            r = app_client.post(f"/activity-board/supplier/{did}/report", data={"csrf_token": csrf}, headers=h,
                                files={"file": ("r.pdf", io.BytesIO(b"%PDF-1.4 x"), "application/pdf")},
                                follow_redirects=False)
            assert "success" in r.headers["location"], r.headers["location"]
        html = app_client.get("/activity-board").text
        # the finished report moves the deal on (Inspection shows Done on the deal page);
        # the Inspection card keeps it, with Reupload
        assert "Reupload" in html
        assert _q(f"select stage from crm_sourcing_deals where id='{did}'")[0] == "pricing_approval"
        st = _q(f"select status, file_path is not null from crm_sourcing_steps s join crm_sourcing_deals d on d.id=s.deal_id "
                f"where d.deal_number='SD-IT{sfx}' and s.step_key='inspection_report'")[0]
        assert st == "done|True", st
        # shared with the deal page
        assert "Inspection Report" in app_client.get(f"/crm/sourcing/{did}").text

        # Customer: Verify Screening -> Started (shown on the deal page too)
        app_client.post(f"/activity-board/customer/{oid}/start", data={"csrf_token": csrf}, headers=h)
        assert "Screening Started" in app_client.get("/activity-board").text
        assert ">Started<" in app_client.get(f"/crm/sales/{oid}").text

        # not assigned -> refused
        did2 = _q(f"select id from crm_sourcing_deals where deal_number='SD-IU{sfx}'")[0]
        r = app_client.post(f"/activity-board/supplier/{did2}/start", data={"csrf_token": csrf}, headers=h,
                            follow_redirects=False)
        assert "error" in r.headers["location"]

        # Verify Credit by the account's mapped AM/DM/GM: new limit + reason, old limit kept
        assert "Verify Credit" in app_client.get("/activity-board?tab=accounts").text
        r = app_client.post(f"/crm/contacts/{cid}/verify-credit", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "new_credit_limit": "75000", "reason": "Good history",
                                  "return_to": "/activity-board?tab=accounts"})
        assert r.headers["location"].startswith("/activity-board?tab=accounts&success"), r.headers["location"]
        got = _q(f"select credit_verified, credit_limit, credit_old_limit, credit_verify_reason from crm_contacts where id='{cid}'")[0]
        assert got == "True|75000.00|50000.00|Good history", got

        # Map Manager: only admin / Activity Board Edit
        r = app_client.post(f"/activity-board/accounts/{cid}/map-manager", data={"csrf_token": csrf}, headers=h,
                            follow_redirects=False)
        assert r.status_code in (302, 303, 403)
        if r.status_code != 403:
            assert "error" in r.headers["location"]

        # admin sees every deal and can map the Manager (the column shows on /crm/contacts too)
        app_client.cookies.clear()
        _login(app_client, admin, apw)
        csrf = app_client.cookies.get("csrf_token") or "x"
        html = app_client.get("/activity-board").text
        assert "SD-IU" + sfx in html and "SD-IT" + sfx in html
        wid = _q(f"select id from users where username='{worker}'")[0]
        r = app_client.post(f"/activity-board/accounts/{cid}/map-manager", headers={"X-CSRF-Token": csrf},
                            data={"csrf_token": csrf, "manager_id": wid}, follow_redirects=False)
        assert "success" in r.headers["location"], r.headers["location"]
        n = _q(f"select count(*) from crm_contact_team_members where contact_id='{cid}' and role_bucket='manager'")[0]
        assert n == "1"
        assert _q(f"select count(*) from crm_contact_team_members where contact_id='{cid}' and role_bucket='am_dm_gm'")[0] == "1"
        lst = app_client.get("/crm/contacts").text
        assert "<th>Manager</th>" in lst
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "(select id from crm_sourcing_deals where deal_number like 'SD-I_{sfx}')"
        await db.execute(text(f"delete from crm_sourcing_steps where deal_id in {{ids}}"))
        await db.execute(text(f"delete from crm_activities where deal_id in {{ids}}"))
        oids = "(select id from crm_sales_opportunities where opp_number = 'OPP-IT{sfx}')"
        await db.execute(text(f"delete from crm_sales_steps where deal_id in {{oids}}"))
        await db.execute(text(f"delete from crm_activities where deal_id in {{oids}}"))
        await db.execute(text("delete from crm_sourcing_deals where deal_number like 'SD-I_{sfx}'"))
        await db.execute(text("delete from crm_sales_opportunities where opp_number = 'OPP-IT{sfx}'"))
        cid = "(select id from crm_contacts where contact_code = 'IT{sfx}')"
        await db.execute(text(f"delete from crm_contact_team_members where contact_id in {{cid}}"))
        await db.execute(text(f"delete from crm_activities where contact_id in {{cid}}"))
        await db.execute(text("delete from crm_contacts where contact_code = 'IT{sfx}'"))
        await db.commit()
asyncio.run(main())
""")
