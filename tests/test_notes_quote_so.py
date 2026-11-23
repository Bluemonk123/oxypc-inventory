"""Notes on Account Quotation and Sales Order (2026-11-23, Pankaj): new
Notes field, shown on the detail page, Sales Order editable there, every
change in Notes History; detail pages carry the Notes History button."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2500:]
    return r.stdout.strip()


def test_notes_quote_so(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    ids = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.crm import CRMContact, CRMQuote, CRMSalesOrder
import services.notes_history  # noqa: F401
async def main():
    async with AsyncSessionLocal() as db:
        c = CRMContact(id=uuid.uuid4(), company_name="NQ Co {sfx}", contact_code="NQ{sfx}")
        db.add(c); await db.flush()
        q = CRMQuote(id=uuid.uuid4(), quote_number="QT-NQ{sfx}", contact_id=c.id, notes="quote note one", created_by="t")
        so = CRMSalesOrder(id=uuid.uuid4(), so_number="SO-NQ{sfx}", contact_id=c.id, quote_id=q.id, created_by="t")
        db.add_all([q, so]); await db.commit()
        print(c.id, q.id, so.id)
asyncio.run(main())
""")
    cid, qid, soid = ids.split()
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        qp = app_client.get(f"/crm/quotes/{qid}").text
        assert "quote note one" in qp and 'data-record-type="quotation"' in qp and 'id="notesHistoryModal"' in qp
        assert 'name="notes"' in app_client.get(f"/crm/quotes/{qid}/edit").text
        sp = app_client.get(f"/crm/sales-orders/{soid}").text
        assert 'action="/crm/sales-orders/' in sp and 'data-record-type="sales_order"' in sp
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/crm/sales-orders/{soid}/notes", data={"csrf_token": csrf, "notes": "deliver Tuesday"},
                            headers={"x-csrf-token": csrf}, follow_redirects=False)
        assert r.status_code == 302
        r = app_client.post(f"/crm/sales-orders/{soid}/notes", data={"csrf_token": csrf, "notes": "deliver Friday instead"},
                            headers={"x-csrf-token": csrf}, follow_redirects=False)
        so_notes = app_client.get(f"/notes-history/api?record_type=sales_order&record_id={soid}").json()["rows"]
        assert [x["note"] for x in so_notes] == ["deliver Friday instead", "deliver Tuesday"]
        qn = app_client.get(f"/notes-history/api?record_type=quotation&record_id={qid}").json()["rows"]
        assert qn[0]["note"] == "quote note one" and qn[0]["note_type"] == "Account Quotation Notes"
        assert 'data-record-type="contact"' in app_client.get(f"/crm/contacts/{cid}").text
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from notes_history where record_id in ('{qid}', '{soid}', '{cid}')"))
        await db.execute(text("delete from audit_logs where record_id in ('{qid}', '{soid}', '{cid}')"))
        await db.execute(text("delete from crm_sales_orders where id = '{soid}'"))
        await db.execute(text("delete from crm_quotes where id = '{qid}'"))
        await db.execute(text("delete from crm_contacts where id = '{cid}'"))
        await db.commit()
asyncio.run(main())
""")
