"""Email Reply-To (2026-11-24, Pankaj): emails come From the sending mailbox
(e.g. renewcircuits@gmail.com) and replies go to OXYPC_SMTP_REPLY_TO
(e.g. info@oxypc.com), editable on Extra Settings → Email."""
import pathlib

from services import email_notify as en
from services.env_settings import EDITABLE_KEYS

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_reply_to_header(monkeypatch):
    monkeypatch.setenv("OXYPC_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("OXYPC_SMTP_USER", "renewcircuits@gmail.com")
    monkeypatch.setenv("OXYPC_SMTP_REPLY_TO", "info@oxypc.com")
    cfg = en.settings()
    assert cfg["reply_to"] == "info@oxypc.com"
    msg = en._build(cfg, ["a@b.com"], [], "Hi", "Body", {})
    assert msg["Reply-To"] == "info@oxypc.com"
    assert "renewcircuits@gmail.com" in msg["From"]


def test_no_reply_to_when_blank(monkeypatch):
    monkeypatch.setenv("OXYPC_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("OXYPC_SMTP_USER", "renewcircuits@gmail.com")
    monkeypatch.delenv("OXYPC_SMTP_REPLY_TO", raising=False)
    msg = en._build(en.settings(), ["a@b.com"], [], "Hi", "Body", {})
    assert msg["Reply-To"] is None


def test_editable_and_on_screen():
    assert "OXYPC_SMTP_REPLY_TO" in EDITABLE_KEYS and "OXYPC_SMTP2_REPLY_TO" in EDITABLE_KEYS
    tpl = (ROOT / "templates/admin/_tab_email.html").read_text()
    assert 'name="reply_to"' in tpl and "Replies go to" in tpl
    src = (ROOT / "routers/master.py").read_text()
    assert 'f"{prefix}_REPLY_TO": reply_to' in src
