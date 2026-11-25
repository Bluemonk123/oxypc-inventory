"""Microsoft 365 (Graph) email method (2026-11-24, Pankaj).

OXYPC_SMTP_METHOD=graph sends through Microsoft Graph with the Tenant ID /
Client ID / Client Secret of an app the Microsoft 365 admin registered — no
mailbox password, no OTP. Covers: settings, the Graph JSON, a mocked send,
plain-words errors, the save route (secret never shown or audited, admin
only, CSRF) and a failed send not breaking the saving request.
"""
import asyncio
import types

import pytest

from services import email_graph, email_notify as en
from services import env_settings

GRAPH_ENV = {"OXYPC_SMTP_METHOD": "graph", "OXYPC_SMTP_TENANT_ID": "tid-1", "OXYPC_SMTP_CLIENT_ID": "cid-1",
             "OXYPC_SMTP_CLIENT_SECRET": "s3cret", "OXYPC_SMTP_FROM": "OxyPC ERP <admin.alerts@oxypc.com>",
             "OXYPC_SMTP_REPLY_TO": "admin.app@oxypc.com"}
ALL_KEYS = [k for k in env_settings.EDITABLE_KEYS]


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    import os
    saved = {k: os.environ.get(k) for k in ALL_KEYS}     # the save route writes os.environ directly
    for k in ALL_KEYS:
        os.environ.pop(k, None)
    monkeypatch.setattr(env_settings, "ENV_PATH", str(tmp_path / ".env"))
    email_graph._tokens.clear()
    yield monkeypatch
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _graph(monkeypatch):
    for k, v in GRAPH_ENV.items():
        monkeypatch.setenv(k, v)


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, str(data)

    def json(self):
        return self._d


def test_graph_settings_and_ready(clean_env):
    _graph(clean_env)
    cfg = en.settings()
    assert cfg["method"] == "graph" and en.is_configured(cfg)
    clean_env.delenv("OXYPC_SMTP_CLIENT_SECRET")
    assert not en.is_configured(en.settings())


def test_payload_from_built_message(clean_env):
    _graph(clean_env)
    cfg = en.settings()
    msg = en._build(cfg, ["ravi@oxypc.com"], [], "Device Assigned to You", "Tag X is yours", {"Tag": "X1"})
    p = email_graph.payload(msg)
    m = p["message"]
    assert m["subject"] == "[OxyPC] Device Assigned to You"
    assert m["toRecipients"] == [{"emailAddress": {"address": "ravi@oxypc.com"}}]
    assert m["replyTo"] == [{"emailAddress": {"address": "admin.app@oxypc.com"}}]
    assert m["body"]["contentType"] == "HTML" and "Tag X is yours" in m["body"]["content"]
    assert p["saveToSentItems"] is False


def test_send_uses_token_and_sender(clean_env):
    _graph(clean_env)
    calls = {"token": 0, "send": []}

    def fake_post(url, timeout, data):
        calls["token"] += 1
        assert "tid-1" in url and data["client_id"] == "cid-1" and data["grant_type"] == "client_credentials"
        return _Resp(200, {"access_token": "T", "expires_in": 3600})

    class FakeClient:
        def __init__(self, timeout, headers):
            assert headers["Authorization"] == "Bearer T"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            calls["send"].append(url)
            return _Resp(202, {})

    clean_env.setattr(email_graph.httpx, "post", fake_post)
    clean_env.setattr(email_graph.httpx, "Client", FakeClient)
    cfg = en.settings()
    msgs = [en._build(cfg, ["a@b.com"], [], "T1", "M", {}), en._build(cfg, ["c@d.com"], [], "T2", "M", {})]
    en._send_sync(cfg, msgs)
    en._send_sync(cfg, msgs[:1])                     # token reused, not fetched again
    assert calls["token"] == 1
    assert calls["send"] == [email_graph.SEND_URL.format(sender="admin.alerts@oxypc.com")] * 3


def test_send_failure_raises_graph_error(clean_env):
    _graph(clean_env)
    clean_env.setattr(email_graph.httpx, "post", lambda url, timeout, data: _Resp(
        401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}))
    with pytest.raises(email_graph.GraphError) as ei:
        en._send_sync(en.settings(), [en._build(en.settings(), ["a@b.com"], [], "T", "M", {})])
    assert "Client Secret is wrong or has expired" in en.friendly(ei.value)


@pytest.mark.parametrize("raw,words", [
    ("(535, b'5.7.139 Authentication unsuccessful, SmtpClientAuthentication is disabled')", "Authenticated SMTP"),
    ("(535, b'5.7.3 Authentication unsuccessful')", "Wrong mailbox name or password"),
    ("554 5.7.60 SendAsDenied", "Sent from address is not the signed-in mailbox"),
    ("[SSL: WRONG_VERSION_NUMBER] wrong version number", "587 = STARTTLS"),
    ("[Errno 111] Connection refused", "Could not reach the mail server"),
    ("Send failed (403 ErrorAccessDenied): Access is denied.", "Mail.Send permission"),
    ("Send failed (404 ErrorInvalidUser): The requested user is invalid.", "mailbox was not found"),
])
def test_friendly_errors(raw, words):
    assert words in en.friendly(Exception(raw))


def test_background_failure_never_raises(clean_env):
    _graph(clean_env)

    def boom(cfg, messages):
        raise email_graph.GraphError("Send failed (403 ErrorAccessDenied)")
    clean_env.setattr(en, "_send_sync", boom)
    # _deliver must swallow the error (the saving request never sees it)
    asyncio.run(en._deliver([{"user_id": None, "title": "t", "message": "m", "barcode": None,
                              "brand": None, "model": None, "stage": None}]))


# ── the save route ──────────────────────────────────────────────────────────
def _admin_client(monkeypatch, app_client, role="admin"):
    import main
    from auth.dependencies import get_current_user
    from models.user import UserRole
    user = types.SimpleNamespace(id=None, username="t_admin", full_name="T Admin", email="t@oxypc.com",
                                 role=UserRole(role), status=True, entity=None)
    main.app.dependency_overrides[get_current_user] = lambda: user
    audits = []

    async def fake_audit(db, **kw):
        audits.append(kw)
    import services.audit_engine as ae
    monkeypatch.setattr(ae, "audit", fake_audit)
    app_client.cookies.set("csrf_token", "tok")
    return main, get_current_user, audits


def test_save_graph_settings_route(clean_env, app_client):
    main, gcu, audits = _admin_client(clean_env, app_client)
    try:
        r = app_client.post("/admin/master/email/settings", follow_redirects=False, data={
            "csrf_token": "tok", "box": "main", "method": "graph", "tenant_id": "tid-9", "client_id": "cid-9",
            "client_secret": "very-secret", "from": "admin.alerts@oxypc.com", "reply_to": "admin.app@oxypc.com"})
        assert r.status_code == 302 and "success" in r.headers["location"]
        cfg = en.settings()
        assert (cfg["method"], cfg["tenant_id"], cfg["client_id"], cfg["client_secret"]) == ("graph", "tid-9", "cid-9", "very-secret")
        assert "very-secret" not in str(audits)                       # secret never audited
        page = app_client.get("/admin/extra-settings?tab=email")
        assert page.status_code == 200 and "very-secret" not in page.text   # never shown
        assert "Microsoft 365 (Graph)" in page.text
        # missing values are refused
        r = app_client.post("/admin/master/email/settings", follow_redirects=False, data={
            "csrf_token": "tok", "box": "backup", "method": "graph", "tenant_id": "x", "from": "a@b.com"})
        assert "error" in r.headers["location"]
        # no CSRF token -> not saved
        r = app_client.post("/admin/master/email/settings", follow_redirects=False, data={
            "box": "main", "method": "graph", "tenant_id": "zzz", "client_id": "c", "client_secret": "s", "from": "a@b.com"})
        assert en.settings()["tenant_id"] == "tid-9"
    finally:
        main.app.dependency_overrides.pop(gcu, None)


def test_test_email_route_admin_only(clean_env, app_client):
    main, gcu, _ = _admin_client(clean_env, app_client, role="sales")
    try:
        r = app_client.post("/admin/master/email/test", follow_redirects=False,
                            data={"csrf_token": "tok", "box": "main", "to": "x@y.com"})
        assert r.status_code in (302, 303, 401, 403) and "success" not in r.headers.get("location", "")
    finally:
        main.app.dependency_overrides.pop(gcu, None)
