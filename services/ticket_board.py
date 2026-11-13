"""Ticket Board (2026-11-13, Pankaj) — helpdesk over the tickets raised from
Raise Ticket, the staff / partner login pages and the partner portal.
Spec: TICKETING_MODULE_SPEC.md (phase 1: list + views, ticket screen,
replies / internal notes, statuses, activity log).

Status flow: new (just arrived, unassigned) -> open (being worked; set
automatically when assigned or first replied) -> pending (waiting on the
requester) / hold (waiting on someone else; SLA clock paused) -> solved.
"""
from sqlalchemy import select, update, func, or_, and_

from models.ticket import Ticket, TicketMessage, STATUSES
from models.user import User
from utils.timezone import app_now

STATUS_LABEL = {"new": "New", "open": "Open", "pending": "Pending", "hold": "On hold", "solved": "Solved"}
STATUS_COLOR = {"new": "#ffb648", "open": "#e34f32", "pending": "#3091ec", "hold": "#2f3941", "solved": "#87929d"}
PRIORITY_COLOR = {"urgent": "#cc3340", "high": "#ed961c", "normal": "#68737d"}
CHANNEL_ICON = {"web": ("bi-globe", "Web form"), "email": ("bi-envelope", "Email"), "agent": ("bi-headset", "Agent")}


async def migrate_legacy_statuses(db) -> int:
    """Open -> new, Closed -> solved (once; later runs find nothing).
    Tickets set Closed keep their last update as the solved time."""
    n1 = (await db.execute(update(Ticket).where(Ticket.status == "Open").values(status="new"))).rowcount or 0
    n2 = (await db.execute(update(Ticket).where(Ticket.status == "Closed")
                           .values(status="solved", solved_at=func.coalesce(Ticket.solved_at, Ticket.updated_at)))).rowcount or 0
    if n1 or n2:
        await db.commit()
    return n1 + n2


def views(me: str):
    """Saved views: (id, label, SQL condition). Order = sidebar order."""
    unsolved = Ticket.status != "solved"
    return [
        ("mine", "Your unsolved tickets", and_(Ticket.assignee == me, unsolved)),
        ("unassigned", "Unassigned tickets", and_(or_(Ticket.assignee.is_(None), Ticket.assignee == ""), unsolved)),
        ("all", "All unsolved tickets", unsolved),
        ("urgent", "Urgent & high priority", and_(Ticket.priority.in_(("urgent", "high")), unsolved)),
        ("email", "Email tickets", and_(Ticket.channel == "email", unsolved)),
        ("pending", "Pending tickets", Ticket.status == "pending"),
        ("solved", "Recently solved", Ticket.status == "solved"),
    ]


def priority_of(t) -> str:
    return t.priority or "normal"


def channel_of(t) -> str:
    return t.channel or "web"


async def user_map(db, usernames) -> dict:
    """username -> User for the given usernames."""
    names = {u for u in usernames if u}
    if not names:
        return {}
    return {u.username: u for u in (await db.execute(select(User).where(User.username.in_(names)))).scalars()}


def requester_info(t, users: dict) -> dict:
    """Who raised it: a staff user, a trade partner ("[DLR-001] Name") or
    someone from the login page (typed name)."""
    raw = t.raised_by or ""
    u = users.get(raw)
    if u:
        return {"name": u.full_name or u.username, "username": u.username, "email": t.requester_email or u.email or "",
                "kind": "Staff", "detail": getattr(u.role, "value", u.role) or "", "user": u}
    if raw.startswith("[") and "] " in raw:
        code, name = raw[1:].split("] ", 1)
        return {"name": name, "username": "", "email": t.requester_email or "", "kind": "Trade partner",
                "detail": code, "user": None}
    return {"name": raw or "—", "username": "", "email": t.requester_email or "", "kind": "Not signed in",
            "detail": "", "user": None}


def add_event(db, t, who, text: str):
    """Activity-log line (grey, centred in the thread)."""
    db.add(TicketMessage(ticket_id=t.id, author_type="system",
                         author_username=getattr(who, "username", None),
                         author_name=(getattr(who, "full_name", None) or getattr(who, "username", None)),
                         body=text))
    t.updated_at = app_now()


def set_status(db, t, new: str, who) -> bool:
    """Change status with its side effects; logs the change. Returns True if changed."""
    if new not in STATUSES or new == t.status:
        return False
    old = t.status
    now = app_now()
    if old == "hold" and t.hold_started_at:            # SLA clock resumes
        t.sla_paused_seconds = (t.sla_paused_seconds or 0) + int((now - t.hold_started_at).total_seconds())
        t.hold_started_at = None
    if new == "hold":
        t.hold_started_at = now
    t.solved_at = now if new == "solved" else None
    t.status = new
    add_event(db, t, who, f"Status changed from {STATUS_LABEL.get(old, old)} to {STATUS_LABEL[new]}")
    return True


async def engineers(db) -> list:
    """Active users who can open the Ticket Board (admins + any role ticked
    for it in the Permission Matrix), with their open-ticket count."""
    from models.role_permissions import has_perm
    users = (await db.execute(select(User).where(User.status == True).order_by(User.full_name))).scalars().all()  # noqa: E712
    load = dict((await db.execute(select(Ticket.assignee, func.count()).where(
        Ticket.status != "solved", Ticket.assignee.isnot(None)).group_by(Ticket.assignee))).all())
    out = []
    for u in users:
        rv = getattr(u.role, "value", u.role)
        if has_perm(str(rv), "ticket_board", "enable"):
            out.append({"username": u.username, "name": u.full_name or u.username, "open": load.get(u.username, 0)})
    return out


# ── SLA (2026-11-13) ─────────────────────────────────────────────────────────
# Due = raised + policy hours for the priority + time spent on hold. Hours
# live in AppSetting (ticket_sla_hours_urgent / _high / _normal); defaults
# 4 / 8 / 24. Badge: green "Xh left", orange under 2h, red "Breached Xh";
# none when solved, "Paused" on hold.
SLA_DEFAULT_HOURS = {"urgent": 4, "high": 8, "normal": 24}


async def sla_hours(db) -> dict:
    from models.settings import AppSetting
    keys = {f"ticket_sla_hours_{p}": p for p in SLA_DEFAULT_HOURS}
    out = dict(SLA_DEFAULT_HOURS)
    for k, v in (await db.execute(select(AppSetting.key, AppSetting.value).where(AppSetting.key.in_(keys)))).all():
        try:
            if float(v) > 0:
                out[keys[k]] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def sla_info(t, hours: dict, now=None) -> dict | None:
    """{'state': ok|warn|breach|paused, 'text': ..., 'left_s': seconds left (negative = late)} or None."""
    from datetime import timedelta
    if t.status == "solved" or not t.raised_on:
        return None
    if t.status == "hold":
        return {"state": "paused", "text": "Paused", "left_s": None}
    now = now or app_now()
    due = t.raised_on + timedelta(hours=hours.get(priority_of(t), 24), seconds=t.sla_paused_seconds or 0)
    left = (due - now).total_seconds()
    if left < 0:
        h = -left / 3600
        return {"state": "breach", "text": f"Breached {round(h)}h" if h >= 1 else f"Breached {round(-left / 60)}m", "left_s": left}
    h = left / 3600
    return {"state": "warn" if h < 2 else "ok", "text": (f"{round(left / 60)}m left" if h < 1 else f"{round(h)}h left"), "left_s": left}
