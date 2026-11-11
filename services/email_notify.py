"""Email notifications (2026-11-09, Pankaj).

Every personal in-app notification is also emailed to that user's email
(User.email). That includes the copies for the user who did the action and
the recipient's Report To manager (services/notifications.py). Broadcasts
(user_id None) stay in-app only — they would email every user each time.
One company mailbox sends all of it. Its login is a one-time setup on the
server in .env (never stored in the database):

    OXYPC_SMTP_HOST      e.g. smtp.gmail.com / smtp.office365.com
    OXYPC_SMTP_PORT      587 (STARTTLS, default) or 465 (SSL)
    OXYPC_SMTP_USER      the mailbox, e.g. erp@oxypc.in
    OXYPC_SMTP_PASSWORD  its password / app password
    OXYPC_SMTP_FROM      optional "OxyPC ERP <erp@oxypc.in>" (default: USER)
    OXYPC_SMTP_SECURITY  optional starttls (default) | ssl | none
    OXYPC_PUBLIC_URL     optional link in the email, e.g. http://192.168.1.100:8000

Backup mailbox (optional, 2026-11-09): the same six settings with SMTP2 —
OXYPC_SMTP2_HOST, OXYPC_SMTP2_PORT, OXYPC_SMTP2_USER, OXYPC_SMTP2_PASSWORD,
OXYPC_SMTP2_FROM, OXYPC_SMTP2_SECURITY. When the main mailbox fails to send
(server down, password changed, sending limit reached), the same emails go
out through the backup instead. Either mailbox can be Google or Microsoft;
any one mailbox can send to any address.

How it hooks in: SQLAlchemy events on the Notification model collect new rows
while a session is open, and after that session COMMITS the emails are sent
in a background task (smtplib in a worker thread), so a page never waits on
the mail server and a rolled-back notification is never emailed. When SMTP is
not configured nothing is sent and nothing fails. Send errors are logged and
never reach the user's request.
"""
import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from html import escape

from sqlalchemy import event, select
from sqlalchemy.orm import Session

log = logging.getLogger("oxypc.email")

BCC_BATCH = 50
_pending_tasks: set = set()   # keep strong refs to running send tasks


def settings(prefix: str = "OXYPC_SMTP") -> dict:
    """SMTP settings from the environment (read each time, so a restart after
    editing .env is all that is needed). prefix OXYPC_SMTP = main mailbox,
    OXYPC_SMTP2 = backup mailbox."""
    g = lambda k: os.environ.get(f"{prefix}_{k}") or ""  # noqa: E731
    port_raw = g("PORT").strip()
    security = g("SECURITY").strip().lower()
    port = int(port_raw) if port_raw.isdigit() else (465 if security == "ssl" else 587)
    if not security:
        security = "ssl" if port == 465 else "starttls"
    user = g("USER").strip()
    return {
        "host": g("HOST").strip(),
        "port": port,
        "user": user,
        "password": g("PASSWORD"),
        "from": g("FROM").strip() or user,
        "security": security if security in ("starttls", "ssl", "none") else "starttls",
        "public_url": (os.environ.get("OXYPC_PUBLIC_URL") or "").strip().rstrip("/"),
    }


def backup_settings() -> dict:
    return settings("OXYPC_SMTP2")


def _ready(cfg: dict) -> bool:
    return bool(cfg["host"] and cfg["from"])


def is_configured(cfg: dict | None = None) -> bool:
    """True when the main or the backup mailbox is set up (or, with cfg
    given, when that one mailbox is)."""
    if cfg is not None:
        return _ready(cfg)
    return _ready(settings()) or _ready(backup_settings())


def _boxes() -> list:
    """Mailboxes to try, in order: main, then backup."""
    return [c for c in (settings(), backup_settings()) if _ready(c)]


def _send_with_fallback(build) -> str:
    """Build and send with the main mailbox; if that fails, with the backup.
    build(cfg) -> list of messages (the From differs per mailbox). Returns
    which mailbox sent; raises the last error when every mailbox fails."""
    last = None
    for name, cfg in zip(("main", "backup"), (settings(), backup_settings())):
        if not _ready(cfg):
            continue
        try:
            _send_sync(cfg, build(cfg))
            return name
        except Exception as e:   # try the next mailbox
            last = e
            log.warning("Email via %s mailbox failed: %s", name, e)
    if last:
        raise last
    raise RuntimeError("No mailbox is set up.")


def _build(cfg: dict, to: list, bcc: list, title: str, message: str, details: dict) -> EmailMessage:
    msg = EmailMessage()
    name, addr = parseaddr(cfg["from"])
    msg["From"] = formataddr((name or "OxyPC ERP", addr or cfg["from"]))
    if to:
        msg["To"] = ", ".join(to)
    else:
        msg["To"] = formataddr((name or "OxyPC ERP", addr or cfg["from"]))  # broadcast: recipients in BCC
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = f"[OxyPC] {title}"
    rows = [(k, v) for k, v in details.items() if v]
    text = [message, ""] + [f"{k}: {v}" for k, v in rows]
    if cfg["public_url"]:
        text += ["", f"Open OxyPC: {cfg['public_url']}"]
    text += ["", "This is an automatic notification from OxyPC ERP."]
    msg.set_content("\n".join(text))
    html_rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#566579">{escape(k)}</td>'
        f'<td style="padding:2px 0"><b>{escape(str(v))}</b></td></tr>' for k, v in rows)
    link = (f'<p><a href="{escape(cfg["public_url"])}" style="color:#0e3a7a">Open OxyPC</a></p>'
            if cfg["public_url"] else "")
    msg.add_alternative(
        f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#14233a">'
        f'<h3 style="color:#0e3a7a;margin:0 0 8px">{escape(title)}</h3>'
        f'<p style="margin:0 0 10px">{escape(message)}</p>'
        f'{"<table>" + html_rows + "</table>" if html_rows else ""}{link}'
        f'<p style="color:#8a94a3;font-size:12px;margin-top:16px">Automatic notification from OxyPC ERP.</p></div>',
        subtype="html")
    return msg


def _send_sync(cfg: dict, messages: list) -> None:
    """Blocking send of several messages over one SMTP connection."""
    if not messages:
        return
    timeout = 20
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout)
    try:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if cfg["user"] and cfg["password"]:
            server.login(cfg["user"], cfg["password"])
        for m in messages:
            server.send_message(m)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_now(to: list, title: str, message: str, details: dict | None = None,
                   box: str = "main") -> str:
    """Send one email right away (the admin "Send test email"). box: "main"
    or "backup" tests that one mailbox. Raises on failure so the caller can
    show the error; returns the mailbox used."""
    cfg = backup_settings() if box == "backup" else settings()
    if not _ready(cfg):
        raise RuntimeError(f"The {box} mailbox is not set up on the server.")
    await asyncio.to_thread(_send_sync, cfg, [_build(cfg, to, [], title, message, details or {})])
    return box


async def _deliver(items: list) -> None:
    """Background task: look up recipients and send. Never raises."""
    try:
        if not _boxes():
            return
        from database import AsyncSessionLocal
        from models.user import User
        async with AsyncSessionLocal() as db:
            ids = {i["user_id"] for i in items if i["user_id"]}
            by_id = {}
            if ids:
                by_id = {u.id: u for u in (await db.execute(
                    select(User).where(User.id.in_(ids)))).scalars().all()}
            everyone = []
            if any(i["user_id"] is None for i in items):
                everyone = [u.email.strip() for u in (await db.execute(
                    select(User).where(User.status == True, User.email.isnot(None))  # noqa: E712
                )).scalars().all() if u.email and "@" in u.email]
        def build(cfg):
            return _messages(cfg, items, by_id, everyone)
        if items:
            await asyncio.to_thread(_send_with_fallback, build)
    except Exception as e:   # mail problems must never break the app
        log.warning("Email notification not sent: %s", e)


def _messages(cfg: dict, items: list, by_id: dict, everyone: list) -> list:
    """The emails for these notification rows, From this mailbox."""
    messages = []
    for i in items:
        details = {"Tag": i.get("barcode"), "Brand": i.get("brand"),
                   "Model": i.get("model"), "Stage": i.get("stage")}
        if i["user_id"]:
            u = by_id.get(i["user_id"])
            if not u or not u.status or not u.email or "@" not in u.email:
                continue
            messages.append(_build(cfg, [u.email.strip()], [], i["title"], i["message"], details))
        else:
            for k in range(0, len(everyone), BCC_BATCH):
                messages.append(_build(cfg, [], everyone[k:k + BCC_BATCH], i["title"], i["message"], details))
    return messages


# ── Hook: new Notification rows -> email after commit ────────────────────────
_KEY = "oxypc_email_notifs"


def _collect(session, flush_context, instances=None):
    """Copy each new Notification's fields now — after the commit the rows
    may be expired, and reloading them there is not allowed."""
    from models.notification import Notification
    for o in session.new:
        if isinstance(o, Notification):
            session.info.setdefault(_KEY, []).append({
                "user_id": o.user_id, "title": o.title or "", "message": o.message or "",
                "barcode": o.barcode, "brand": o.brand, "model": o.model, "stage": o.stage})


def _after_commit(session):
    items = [i for i in (session.info.pop(_KEY, None) or []) if i["user_id"]]   # personal only
    if not items or not is_configured():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # no event loop (scripts / tests): skip quietly
    task = loop.create_task(_deliver(items))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def _after_rollback(session, *args):
    session.info.pop(_KEY, None)


_installed = False


def install() -> None:
    """Register the session hooks once (called from main.py at startup)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _collect)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_soft_rollback", _after_rollback)
    _installed = True
