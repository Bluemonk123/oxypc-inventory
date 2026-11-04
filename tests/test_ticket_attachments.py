"""Raise Ticket attachments (2026-11-04): images, PDF, MS Office and ZIP files
are accepted (checked by extension AND the file's first bytes); anything else,
or a file whose contents don't match its name, is dropped. The ticket list
shows an icon tile for non-image files."""
import io
import os

from tests.test_customise_ram_storage import _run, ROOT
from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_ticket_accepts_pdf_office_zip_and_refuses_fakes(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    files = [
        ("photos", ("report.pdf", io.BytesIO(b"%PDF-1.7\n%test\n"), "application/pdf")),
        ("photos", ("logs.zip", io.BytesIO(b"PK\x03\x04" + b"\0" * 40), "application/x-zip-compressed")),
    ]
    r = app_client.post("/tickets/raise", data={"feedback": "itest attachments ok", "csrf_token": csrf},
                        files=files, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json().get("ok"), r.text
    tid = r.json()["ticket_id"]
    fake = [("photos", ("evil.pdf", io.BytesIO(b"<html><script>alert(1)</script>"), "application/pdf")),
            ("photos", ("page.html", io.BytesIO(b"<html></html>"), "text/html"))]
    r2 = app_client.post("/tickets/raise", data={"feedback": "itest attachments fake", "csrf_token": csrf},
                         files=fake, headers={"X-CSRF-Token": csrf})
    tid2 = r2.json()["ticket_id"]
    try:
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for t in ("{tid}", "{tid2}"):
            print((await db.execute(text("select coalesce(photo1_path,'-')||'|'||coalesce(photo2_path,'-') from tickets where ticket_id = :t"), {{"t": t}})).scalar())
asyncio.run(main())
""").splitlines()
        p1, p2 = out[0].split("|")
        assert p1.endswith(".pdf") and p2.endswith(".zip"), out
        assert out[1] == "-|-", out
        html = app_client.get("/tickets").text
        assert "bi-file-earmark-pdf" in html and "bi-file-earmark-zip" in html
    finally:
        for p in out[0].split("|") if 'out' in dir() else []:
            if p != "-" and os.path.exists(os.path.join(ROOT, p.lstrip("/"))):
                os.remove(os.path.join(ROOT, p.lstrip("/")))   # files this test wrote
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from tickets where ticket_id in ('{tid}', '{tid2}')"))
        await db.commit()
asyncio.run(main())
""")
