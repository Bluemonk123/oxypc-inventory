"""Ticket Board (2026-11-13, Pankaj): /ticket-board — the helpdesk view of
raised tickets. Phase 1: saved views + list, ticket screen (conversation,
properties, requester context), New ticket, public replies / internal notes
with "Submit as <status>", and the activity log. Logic: services/ticket_board.py.

Access: Permission Matrix module "ticket_board" (admin only until ticked).
Add = New ticket; Edit = reply / note / change fields.
"""
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, verify_csrf, require_module_perm
from database import get_db
from models.ticket import Ticket, TicketMessage, STATUSES, PRIORITIES, TYPES
from models.user import User
from services.audit_engine import audit
from services import ticket_board as tb
from templates_config import templates
from utils.master_data import master_options
from utils.timezone import app_now

router = APIRouter(prefix="/ticket-board", tags=["ticket-board"], dependencies=[Depends(verify_csrf)])
page_perm = require_module_perm("ticket_board")


def _groups():
    return master_options("ticket_group") or ["Development", "Support"]


def _back(ticket_id, msg="", ok=True):
    url = f"/ticket-board/{ticket_id}"
    if msg:
        url += ("?success=" if ok else "?error=") + quote_plus(msg)
    return RedirectResponse(url=url, status_code=302)


async def _notify(db, username, title, message, kind="info"):
    """Personal alert to a staff user (also emailed when SMTP is set). Never fails the request."""
    if not username:
        return
    try:
        uid = (await db.execute(select(User.id).where(User.username == username, User.status == True))).scalar_one_or_none()  # noqa: E712
        if uid:
            from services.notifications import create_notification
            await create_notification(db, user_id=uid, title=title, message=message, notification_type=kind)
            await db.commit()
    except Exception:
        await db.rollback()


# ── LIST ─────────────────────────────────────────────────────────────────────

async def _items(db, rows) -> list:
    """Table rows for a list of tickets: requester, assignee name, preview
    (latest reply / note, else the ticket text), priority, channel, SLA."""
    ids = [t.id for t in rows]
    last = {}
    if ids:
        sub = (select(TicketMessage.ticket_id, func.max(TicketMessage.created_at).label("m"))
               .where(TicketMessage.ticket_id.in_(ids), TicketMessage.author_type != "system")
               .group_by(TicketMessage.ticket_id).subquery())
        for m in (await db.execute(select(TicketMessage).join(
                sub, (TicketMessage.ticket_id == sub.c.ticket_id) & (TicketMessage.created_at == sub.c.m)))).scalars():
            last[m.ticket_id] = m.body or ""
    users = await tb.user_map(db, {t.raised_by for t in rows} | {t.assignee for t in rows})
    hours, now = await tb.sla_hours(db), app_now()
    items = []
    for t in rows:
        a = users.get(t.assignee)
        items.append({"t": t, "req": tb.requester_info(t, users),
                      "assignee": (a.full_name or a.username) if a else (t.assignee or ""),
                      "preview": (last.get(t.id) or t.feedback or "")[:160],
                      "priority": tb.priority_of(t), "channel": tb.channel_of(t), "sla": tb.sla_info(t, hours, now)})
    return items


@router.get("", response_class=HTMLResponse)
async def board(request: Request, view: str = "", priority: str = "", group: str = "",
                db: AsyncSession = Depends(get_db), current_user: User = Depends(page_perm)):
    vs = tb.views(current_user.username)
    counts = {}
    for vid, _, cond in vs:
        counts[vid] = (await db.execute(select(func.count()).select_from(Ticket).where(cond))).scalar() or 0
    if not view:
        view = "mine" if counts.get("mine") else "all"
    cur = next((v for v in vs if v[0] == view), vs[2])
    q = select(Ticket).where(cur[2])
    if priority in PRIORITIES:
        q = q.where(Ticket.priority == priority) if priority != "normal" else \
            q.where(or_(Ticket.priority.is_(None), Ticket.priority == "normal"))
    if group:
        q = q.where(Ticket.group_name == group)
    rows = (await db.execute(q.order_by(Ticket.updated_at.desc().nullslast()).limit(2000))).scalars().all()
    items = await _items(db, rows)
    all_users = (await db.execute(select(User.username, User.full_name, User.email).where(User.status == True)  # noqa: E712
                                  .order_by(User.full_name))).all()
    return templates.TemplateResponse("ticket_board/board.html", {
        "request": request, "current_user": current_user, "views": [(v[0], v[1]) for v in vs], "counts": counts,
        "view": cur[0], "view_label": cur[1], "items": items, "priority": priority, "group": group,
        "groups": _groups(), "PRIORITIES": PRIORITIES, "TYPES": TYPES, "tb": tb, "all_users": all_users,
    })


# ── DASHBOARD ────────────────────────────────────────────────────────────────
# Support dashboard (2026-11-13, Pankaj; spec phase 4): 8 cards — Unsolved,
# Unassigned, SLA breached, Solved, Total, Urgent / High / Normal (unsolved)
# — the clicked card's tickets right under them, then 4 bar charts.

DASH_CARDS = [
    ("unsolved", "Unsolved tickets", "#0d6efd"), ("unassigned", "Unassigned", "#6c757d"),
    ("breached", "SLA breached", "#cc3340"), ("solved", "Solved", "#038153"),
    ("total", "Total tickets", "#212529"), ("urgent", "Urgent · unsolved", "#cc3340"),
    ("high", "High · unsolved", "#ed961c"), ("normal", "Normal · unsolved", "#68737d"),
]


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, card: str = "unsolved", db: AsyncSession = Depends(get_db),
                    current_user: User = Depends(page_perm)):
    rows = (await db.execute(select(Ticket).order_by(Ticket.updated_at.desc().nullslast()))).scalars().all()
    hours, now = await tb.sla_hours(db), app_now()
    unsolved = [t for t in rows if t.status != "solved"]
    sla = {t.id: tb.sla_info(t, hours, now) for t in unsolved}
    tests = {
        "unsolved": lambda t: t.status != "solved",
        "unassigned": lambda t: t.status != "solved" and not t.assignee,
        "breached": lambda t: (sla.get(t.id) or {}).get("state") == "breach",
        "solved": lambda t: t.status == "solved",
        "total": lambda t: True,
        "urgent": lambda t: t.status != "solved" and tb.priority_of(t) == "urgent",
        "high": lambda t: t.status != "solved" and tb.priority_of(t) == "high",
        "normal": lambda t: t.status != "solved" and tb.priority_of(t) == "normal",
    }
    if card not in tests:
        card = "unsolved"
    counts = {k: sum(1 for t in rows if f(t)) for k, f in tests.items()}
    chosen = [t for t in rows if tests[card](t)]
    items = await _items(db, chosen[:2000])
    total = len(rows) or 1
    by = lambda key, vals: [(v, sum(1 for t in rows if key(t) == v)) for v in vals]  # noqa: E731
    engs = await tb.engineers(db)
    charts = {
        "status": by(lambda t: t.status, ["new", "open", "pending", "hold", "solved"]),
        "priority": by(tb.priority_of, list(PRIORITIES)),
        "group": by(lambda t: t.group_name or "—", _groups() + (["—"] if any(not t.group_name for t in rows) else [])),
        "engineer": sorted([(e["name"], e["open"]) for e in engs if e["open"]] or [], key=lambda x: -x[1]),
    }
    return templates.TemplateResponse("ticket_board/dashboard.html", {
        "request": request, "current_user": current_user, "tb": tb, "cards": DASH_CARDS, "counts": counts,
        "card": card, "card_label": dict((c[0], c[1]) for c in DASH_CARDS)[card], "items": items,
        "charts": charts, "total": total, "open_total": max(1, len(unsolved)),
    })


# ── NEW TICKET (agent) ───────────────────────────────────────────────────────

@router.post("/new")
async def new_ticket(
    request: Request,
    requester_username: str = Form(""),
    requester_name: str = Form(""),
    requester_email: str = Form(""),
    subject: str = Form(...),
    priority: str = Form("normal"),
    ticket_type: str = Form("new issue"),
    group_name: str = Form(""),
    description: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from routers.tickets import _next_ticket_id
    raised_by = ""
    if requester_username:
        u = (await db.execute(select(User).where(User.username == requester_username))).scalar_one_or_none()
        if u:
            raised_by = u.username
            requester_email = requester_email or (u.email or "")
    if not raised_by:
        raised_by = " ".join(requester_name.split()).lstrip("[ ")[:50]
    if not raised_by or not subject.strip() or not description.strip():
        return RedirectResponse(url="/ticket-board?error=" + quote_plus("Requester, subject and description are required"),
                                status_code=302)
    t = Ticket(ticket_id=await _next_ticket_id(db), raised_by=raised_by, feedback=description.strip()[:5000],
               subject=subject.strip()[:200], status="new", channel="agent",
               priority=priority if priority in PRIORITIES else "normal",
               ticket_type=ticket_type if ticket_type in TYPES else "new issue",
               group_name=group_name if group_name in _groups() else "Support",
               requester_email=(requester_email or "").strip()[:150] or None)
    db.add(t)
    await db.flush()
    tb.add_event(db, t, current_user, f"Ticket created by {current_user.full_name or current_user.username}")
    await audit(db, action="TICKET_CREATED", user=current_user, table_name="tickets", record_id=str(t.id),
                new_value={"ticket_id": t.ticket_id, "subject": t.subject, "channel": "agent"}, request=request)
    await db.commit()
    return _back(t.ticket_id, f"Ticket {t.ticket_id} created")


# ── TICKET SCREEN ────────────────────────────────────────────────────────────

async def _get(db, ticket_id) -> Ticket | None:
    return (await db.execute(select(Ticket).where(Ticket.ticket_id == ticket_id))).scalar_one_or_none()


@router.get("/{ticket_id}", response_class=HTMLResponse)
async def ticket_page(request: Request, ticket_id: str, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(page_perm)):
    t = await _get(db, ticket_id)
    if not t:
        return RedirectResponse(url="/ticket-board?error=" + quote_plus(f"Ticket {ticket_id} not found"), status_code=302)
    msgs = (await db.execute(select(TicketMessage).where(TicketMessage.ticket_id == t.id)
                             .order_by(TicketMessage.created_at))).scalars().all()
    others = (await db.execute(select(Ticket).where(Ticket.raised_by == t.raised_by, Ticket.id != t.id)
                               .order_by(Ticket.raised_on.desc()).limit(20))).scalars().all()
    users = await tb.user_map(db, {t.raised_by, t.assignee} | {m.author_username for m in msgs})
    from routers.tickets import attachment_kind
    first_files = [{"path": p, "kind": attachment_kind(p), "name": p.rsplit("/", 1)[-1]}
                   for p in (t.photo1_path, t.photo2_path) if p]
    return templates.TemplateResponse("ticket_board/ticket.html", {
        "request": request, "current_user": current_user, "t": t, "msgs": msgs, "others": others,
        "req": tb.requester_info(t, users), "users": users, "engineers": await tb.engineers(db),
        "groups": _groups(), "STATUSES": STATUSES, "PRIORITIES": PRIORITIES, "TYPES": TYPES, "tb": tb,
        "first_files": first_files, "priority": tb.priority_of(t), "channel": tb.channel_of(t),
        "assignee_user": users.get(t.assignee), "sla": tb.sla_info(t, await tb.sla_hours(db)),
    })


# ── REPLY / NOTE ─────────────────────────────────────────────────────────────

@router.post("/{ticket_id}/reply")
async def reply(request: Request, ticket_id: str, body: str = Form(""), is_internal: str = Form(""),
                submit_status: str = Form(""), db: AsyncSession = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    t = await _get(db, ticket_id)
    if not t:
        return RedirectResponse(url="/ticket-board", status_code=302)
    text = (body or "").strip()[:20000]
    internal = is_internal in ("1", "true", "on")
    if not text and (not submit_status or submit_status == t.status):
        return _back(ticket_id, "Write a reply or change the status", ok=False)
    me = current_user.full_name or current_user.username
    if text:
        db.add(TicketMessage(ticket_id=t.id, author_type="user", author_username=current_user.username,
                             author_name=me, body=text, is_internal=internal))
    if not t.assignee and text:                      # replying to an unassigned ticket takes it
        t.assignee = current_user.username
        tb.add_event(db, t, current_user, f"Assigned to {me} (replied)")
    target = submit_status if submit_status in STATUSES else t.status
    if target == "new" and (t.assignee or text):     # new moves to open once worked
        target = "open"
    tb.set_status(db, t, target, current_user)
    t.updated_at = app_now()
    await db.commit()
    if text and not internal and t.raised_by != current_user.username:
        await _notify(db, t.raised_by, f"Reply on ticket {t.ticket_id}",
                      f"{me}: " + (text if len(text) <= 160 else text[:157] + "..."))
    kind = "Internal note added" if internal and text else ("Reply sent" if text else "Ticket updated")
    return _back(ticket_id, f"{kind} — {tb.STATUS_LABEL[t.status]}")


# ── PROPERTIES ───────────────────────────────────────────────────────────────

FIELDS = {"assignee": "Assignee", "group_name": "Group", "ticket_type": "Type", "priority": "Priority", "subject": "Subject"}


@router.post("/{ticket_id}/update")
async def update_field(request: Request, ticket_id: str, field: str = Form(...), value: str = Form(""),
                       db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    t = await _get(db, ticket_id)
    if not t or field not in FIELDS:
        return _back(ticket_id, "Nothing to change", ok=False)
    value = (value or "").strip()
    if field == "priority" and value not in PRIORITIES:
        return _back(ticket_id, "Pick urgent, high or normal", ok=False)
    if field == "ticket_type" and value not in TYPES:
        return _back(ticket_id, "Pick a type", ok=False)
    if field == "group_name" and value not in _groups():
        return _back(ticket_id, "Pick a group", ok=False)
    users = {}
    if field == "assignee" and value:
        users = await tb.user_map(db, {value, t.assignee})
        if value not in users:
            return _back(ticket_id, "Pick an engineer", ok=False)
    if field == "subject":
        value = value[:200]
        if not value:
            return _back(ticket_id, "Subject can't be blank", ok=False)
    old = getattr(t, field) or ("normal" if field == "priority" else "")
    if old == value:
        return _back(ticket_id)
    setattr(t, field, value or None)
    label = FIELDS[field]
    if field == "assignee":
        users = users or await tb.user_map(db, {old})
        name = lambda u: (users[u].full_name or u) if u in users else (u or "Unassigned")  # noqa: E731
        tb.add_event(db, t, current_user, f"{label} changed from {name(old)} to {name(value)}" if old
                     else f"Assigned to {name(value)}")
        if value and t.status == "new":
            tb.set_status(db, t, "open", current_user)
    else:
        tb.add_event(db, t, current_user, f"{label} changed from {old or '—'} to {value}")
    await audit(db, action="TICKET_UPDATED", user=current_user, table_name="tickets", record_id=str(t.id),
                old_value={field: old}, new_value={field: value}, request=request)
    await db.commit()
    if field == "assignee" and value and value != current_user.username:
        await _notify(db, value, f"Ticket {t.ticket_id} assigned to you",
                      f"{current_user.full_name or current_user.username} assigned you: {t.display_subject}")
    return _back(ticket_id, f"{label} updated")
