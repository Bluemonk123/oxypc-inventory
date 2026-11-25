"""Send email through Microsoft 365 with Microsoft Graph (2026-11-24, Pankaj).

No mailbox password and no OTP: the Microsoft 365 admin registers an app
("OxyPC ERP") with the Mail.Send application permission and gives three
values — Tenant ID, Client ID, Client Secret. OxyPC exchanges them for a
short-lived token and sends with one HTTPS call per email:

    POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
    POST https://graph.microsoft.com/v1.0/users/<sender>/sendMail

Not affected by Microsoft switching off password sign-in for SMTP (end of
December 2026). The server needs outgoing HTTPS to login.microsoftonline.com
and graph.microsoft.com.

Used by services/email_notify.py when a mailbox's method is "graph"; the
EmailMessage built there is turned into Graph's JSON here, so the content is
identical to the SMTP path.
"""
from __future__ import annotations

import threading
import time
from email.message import EmailMessage
from email.utils import getaddresses

import httpx

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
TIMEOUT = 20

_tokens: dict = {}            # (tenant, client_id, secret) -> (token, expires_at)
_lock = threading.Lock()


class GraphError(Exception):
    """A Microsoft sign-in or send failure; str() is the server's own text."""


def _token(cfg: dict) -> str:
    key = (cfg["tenant_id"], cfg["client_id"], cfg["client_secret"])
    with _lock:
        tok = _tokens.get(key)
        if tok and tok[1] - 60 > time.time():
            return tok[0]
    try:
        r = httpx.post(TOKEN_URL.format(tenant=cfg["tenant_id"]), timeout=TIMEOUT, data={
            "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
            "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
    except httpx.HTTPError as e:
        raise GraphError(f"Could not reach Microsoft sign-in: {type(e).__name__}: {e}") from e
    data = _json(r)
    if r.status_code != 200 or "access_token" not in data:
        raise GraphError(f"Sign-in failed ({r.status_code}): {data.get('error_description') or data.get('error') or r.text[:300]}")
    with _lock:
        _tokens[key] = (data["access_token"], time.time() + int(data.get("expires_in") or 3600))
    return data["access_token"]


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


def _addrs(value) -> list:
    return [{"emailAddress": {"address": a, **({"name": n} if n else {})}}
            for n, a in getaddresses([value or ""]) if a and "@" in a]


def payload(msg: EmailMessage) -> dict:
    """Graph sendMail JSON for an EmailMessage built by email_notify._build."""
    html = msg.get_body(preferencelist=("html",))
    text = msg.get_body(preferencelist=("plain",))
    body = ({"contentType": "HTML", "content": html.get_content()} if html is not None
            else {"contentType": "Text", "content": text.get_content() if text is not None else ""})
    m = {"subject": str(msg["Subject"] or ""), "body": body,
         "toRecipients": _addrs(msg["To"]), "bccRecipients": _addrs(msg["Bcc"])}
    if msg["Reply-To"]:
        m["replyTo"] = _addrs(msg["Reply-To"])
    return {"message": m, "saveToSentItems": False}


def send(cfg: dict, messages: list) -> None:
    """Blocking send of several messages from cfg['sender']."""
    if not messages:
        return
    token = _token(cfg)
    url = SEND_URL.format(sender=cfg["sender"])
    with httpx.Client(timeout=TIMEOUT, headers={"Authorization": f"Bearer {token}"}) as client:
        for msg in messages:
            try:
                r = client.post(url, json=payload(msg))
            except httpx.HTTPError as e:
                raise GraphError(f"Could not reach Microsoft Graph: {type(e).__name__}: {e}") from e
            if r.status_code != 202:
                err = _json(r).get("error") or {}
                raise GraphError(f"Send failed ({r.status_code} {err.get('code', '')}): {err.get('message') or r.text[:300]}")
