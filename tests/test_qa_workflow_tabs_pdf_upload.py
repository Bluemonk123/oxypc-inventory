"""QA Workflow (2026-11-04): tabs New GRN / Re-GRN / Stage Flows, PDF downloads
(diagram, journey, stage flows) and an uploaded Figma diagram per tab
(PNG / JPG / SVG / PDF; fake files and SVGs with scripts refused)."""
import base64
import io
import os

from tests.test_customise_ram_storage import ROOT
from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _post(c, url, files=None):
    csrf = c.cookies.get("csrf_token") or "dummy"
    return c.post(url, data={"csrf_token": csrf}, files=files, headers={"X-CSRF-Token": csrf})


def test_tabs_and_pdfs(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    html = app_client.get("/qa/workflow?tab=regrn").text
    for t in ('data-bs-target="#wf_new_grn"', 'data-bs-target="#wf_regrn"', 'data-bs-target="#wf_stage"'):
        assert t in html
    assert 'id="wf_regrn"' in html and 'tab-pane fade show active" id="wf_regrn"' in html
    for url in ("/qa/workflow/new_grn/diagram.pdf", "/qa/workflow/regrn/diagram.pdf",
                "/qa/workflow/new_grn/journey.pdf", "/qa/workflow/regrn/journey.pdf", "/qa/workflow/stage/journey.pdf"):
        r = app_client.get(url)
        assert r.status_code == 200 and r.content[:5] == b"%PDF-", url
        assert "attachment" in r.headers.get("content-disposition", "")
    assert app_client.get("/qa/workflow/nope/diagram.pdf").status_code == 404


def test_upload_replace_refuse_remove(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    written = []
    try:
        r = _post(app_client, "/qa/workflow/regrn/upload", files={"file": ("flow.png", io.BytesIO(PNG), "image/png")})
        assert r.status_code == 200 and r.json()["ok"], r.text
        written.append(r.json()["upload"]["file"])
        v = app_client.get("/qa/workflow/regrn/uploaded")
        assert v.status_code == 200 and v.headers["content-type"] == "image/png"
        assert "default-src 'none'" in v.headers.get("content-security-policy", "")
        html = app_client.get("/qa/workflow?tab=regrn").text
        assert '/qa/workflow/regrn/uploaded' in html and "Replace diagram" in html
        # refused: name says PNG, contents are HTML; SVG with a script; SVG with an onload
        for name, data in (("x.png", b"<html><script>1</script></html>"),
                           ("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
                           ("y.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect/></svg>')):
            r = _post(app_client, "/qa/workflow/regrn/upload", files={"file": (name, io.BytesIO(data), "image/svg+xml")})
            assert r.status_code == 400 and not r.json()["ok"], name
        # a clean SVG (what Figma exports) and a PDF are accepted
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="#0e3a7a"/></svg>'
        r = _post(app_client, "/qa/workflow/stage/upload", files={"file": ("f.svg", io.BytesIO(svg), "image/svg+xml")})
        assert r.json()["ok"], r.text
        written.append(r.json()["upload"]["file"])
        r = _post(app_client, "/qa/workflow/new_grn/upload", files={"file": ("f.pdf", io.BytesIO(b"%PDF-1.4\n%%EOF"), "application/pdf")})
        assert r.json()["ok"], r.text
        written.append(r.json()["upload"]["file"])
        assert "<iframe" in app_client.get("/qa/workflow?tab=new_grn").text
    finally:
        for k in ("regrn", "stage", "new_grn"):
            _post(app_client, f"/qa/workflow/{k}/upload/remove")
        assert app_client.get("/qa/workflow/regrn/uploaded").status_code == 404
        for f in written:                       # files this test wrote
            p = os.path.join(ROOT, "uploads", "qa_flows", f)
            if os.path.exists(p):
                os.remove(p)


def test_upload_needs_qa_upload_tick(app_client, make_user):  # noqa: F811
    import uuid
    from models.role_permissions import set_cached_perms
    role = "itest_qaup_" + uuid.uuid4().hex[:6]
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        set_cached_perms(role, {"qa": {"enable": True, "upload": False, "delete": False, "export": False}})
        html = app_client.get("/qa/workflow").text
        assert 'class="btn btn-sm btn-outline-primary wf-up-btn"' not in html and 'href="/qa/workflow/new_grn/diagram.pdf"' not in html
        r = _post(app_client, "/qa/workflow/regrn/upload", files={"file": ("flow.png", io.BytesIO(PNG), "image/png")})
        assert r.status_code in (303, 403) or not r.json().get("ok")
    finally:
        set_cached_perms(role, {})


def test_qa_writes_need_csrf(app_client, make_user):  # noqa: F811
    """2026-11-04: every QA POST checks the CSRF token (router-level)."""
    import uuid
    from tests.test_customise_ram_storage import _run
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    title = "ITCSRF " + uuid.uuid4().hex[:6]
    r = app_client.post("/qa/requirements/new", data={"title": title}, follow_redirects=False)
    # no token: refused the app's way (back to login), nothing saved
    assert r.status_code in (302, 303) and r.headers["location"] == "/auth/login", (r.status_code, r.headers.get("location"))
    assert title not in app_client.get("/qa/requirements").text
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    r = app_client.post("/qa/requirements/new", data={"title": title, "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/qa/requirements"), r.headers.get("location")
    assert title in app_client.get("/qa/requirements").text            # with the form's token: saved
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from qa_requirements where title = '{title}'"))
        await db.commit()
asyncio.run(main())
""")
