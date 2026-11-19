"""Ticket raising system — users log feedback, can see own tickets."""
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from limiter import limiter, ip_key_func
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User
from models.ticket import Ticket, DEFAULT_GROUP
from utils.timezone import app_now

router = APIRouter(
    prefix="/tickets",
    tags=["tickets"],
    dependencies=[Depends(verify_csrf)],
)


async def _next_ticket_id(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(Ticket.id)))
    n = (result.scalar() or 0) + 1
    return str(10000000 + n)  # Always 8 digits, starts at 10000001


# Attachments (2026-11-04): images, PDF, MS Office files and ZIP. Checked by
# file extension AND the file's first bytes (its real format), never by the
# browser's content type or the name alone. Anything that could be served as
# active content (HTML, SVG, scripts, macro-enabled Office .xlsm/.docm) is
# refused rather than sanitised. Saved under our own name + vetted extension.
ALLOWED_PHOTO_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}   # kept for imports
# extension -> magic-byte family
ATTACH_EXT = {
    "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "webp": "webp",
    "pdf": "pdf",
    "doc": "ole", "xls": "ole", "ppt": "ole",            # Office 97-2003
    "docx": "zip", "xlsx": "zip", "pptx": "zip",         # Office 2007+ (ZIP containers)
    "zip": "zip",
}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_PHOTOS = 2
MAX_PHOTO_BYTES = 5 * 1024 * 1024


def _magic_ok(head: bytes, family: str) -> bool:
    return {
        "png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": head.startswith(b"\xff\xd8\xff"),
        "gif": head[:6] in (b"GIF87a", b"GIF89a"),
        "webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP",
        "pdf": head.startswith(b"%PDF-"),
        "ole": head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        "zip": head[:4] in (b"PK\x03\x04", b"PK\x05\x06"),
    }.get(family, False)


def attachment_kind(path: str) -> str:
    """'image' / 'pdf' / 'word' / 'excel' / 'powerpoint' / 'zip' for the ticket list."""
    ext = (path or "").rsplit(".", 1)[-1].lower()
    if ext in IMAGE_EXT:
        return "image"
    return {"pdf": "pdf", "doc": "word", "docx": "word", "xls": "excel", "xlsx": "excel",
            "ppt": "powerpoint", "pptx": "powerpoint", "zip": "zip"}.get(ext, "file")


async def _save_photo(photo: UploadFile, ticket_id: str, idx: int) -> str | None:
    """Persist one attachment and return its /uploads path, or None if unusable."""
    if not photo or not photo.filename:
        return None
    ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
    family = ATTACH_EXT.get(ext)
    if not family:
        return None
    head = await photo.read(16)
    await photo.seek(0)
    if not _magic_ok(head, family):
        return None          # name says one thing, contents another
    if ext == "jpeg":
        ext = "jpg"
    upload_dir = Path("uploads/tickets")
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Saved under our own name; only the vetted extension is kept from the
    # user's filename (which is attacker-controlled).
    dest = upload_dir / f"tkt_{ticket_id}_{idx}_{int(time.time())}.{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(photo.file, f)
    if dest.stat().st_size > MAX_PHOTO_BYTES:
        dest.unlink(missing_ok=True)   # our own just-written temp, not user data
        return None
    return "/" + str(dest).replace("\\", "/")


def _age_pill(raised_on) -> tuple[str, str]:
    """Return (label, bootstrap colour class) for ticket ageing."""
    if not raised_on:
        return "—", "secondary"
    now = app_now()
    days = max(0, (now - raised_on).days)
    if days == 0:
        return "Today", "success"
    if days <= 7:
        return f"{days}d", "info text-dark"
    if days <= 30:
        return f"{days}d", "warning text-dark"
    return f"{days}d", "danger"


def _ticket_ctx(t: Ticket, names: dict | None = None) -> dict:
    age_label, age_cls = _age_pill(t.raised_on)
    return {
        "ticket_id":  t.ticket_id,
        "status":     t.simple_status,   # Open / Closed (Ticket Board has the full status)
        "raised_on":  t.raised_on.strftime("%d-%m-%Y %H:%M") if t.raised_on else "—",
        "raised_by":  t.raised_by,
        # Full display name (2026-10-16); falls back to the stored username
        # when the user no longer exists.
        "raised_by_name": (names or {}).get(t.raised_by) or t.raised_by,
        "feedback":   t.feedback or "",
        "notes":      t.notes or "",
        "photos":     [p for p in (t.photo1_path, t.photo2_path) if p],
        "attachments": [{"path": p, "kind": attachment_kind(p)} for p in (t.photo1_path, t.photo2_path) if p],
        "age_label":  age_label,
        "age_cls":    age_cls,
        "age_days":   max(0, (app_now() - t.raised_on).days) if t.raised_on else 0,
        "raised_iso": t.raised_on.isoformat() if t.raised_on else "",
    }


# ── LIST ─────────────────────────────────────────────────────────────────────

def _parse_day(v: str):
    from datetime import datetime as _d
    try:
        return _d.strptime((v or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


async def _filtered_tickets(db, current_user, raised_from: str, raised_to: str, raised_by: str):
    """Tickets for the list and for Convert to Task (2026-11-19, Pankaj):
    non-admins see only their own; Raised On from/to (inclusive days) and
    Raised By (username) filter both the same way."""
    from datetime import timedelta
    query = select(Ticket).order_by(Ticket.raised_on.desc())
    if current_user.role.value != "admin":
        query = query.where(Ticket.raised_by == current_user.username)
    d_from, d_to = _parse_day(raised_from), _parse_day(raised_to)
    if d_from:
        query = query.where(Ticket.raised_on >= d_from)
    if d_to:
        query = query.where(Ticket.raised_on < d_to + timedelta(days=1))
    if (raised_by or "").strip():
        query = query.where(Ticket.raised_by == raised_by.strip())
    return (await db.execute(query)).scalars().all()


async def _user_names(db, usernames) -> dict:
    usernames = {u for u in usernames if u}
    if not usernames:
        return {}
    return {u: n for u, n in (await db.execute(
        select(User.username, User.full_name).where(User.username.in_(usernames)))).all()}


@router.get("", response_class=HTMLResponse)
async def list_tickets(
    request: Request,
    raised_from: str = "",
    raised_to: str = "",
    raised_by: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = await _filtered_tickets(db, current_user, raised_from, raised_to, raised_by)
    # Raised By filter options: everyone who has raised a ticket this user can see.
    by_q = select(Ticket.raised_by).distinct()
    if current_user.role.value != "admin":
        by_q = by_q.where(Ticket.raised_by == current_user.username)
    raisers = [u for u in (await db.execute(by_q)).scalars().all() if u]
    names = await _user_names(db, set(raisers) | {t.raised_by for t in rows})
    raiser_opts = sorted(((u, names.get(u) or u) for u in raisers), key=lambda x: x[1].lower())
    tickets = [_ticket_ctx(t, names) for t in rows]
    # Dark tiles (2026-11-10, Pankaj): Total / Open / Closed / Average Aging.
    # Average Aging = mean days since raised, over the tickets still Open.
    open_t = [t for t in tickets if t["status"] == "Open"]
    stats = {
        "total": len(tickets),
        "open": len(open_t),
        "closed": len(tickets) - len(open_t),
        "avg_age": round(sum(t["age_days"] for t in open_t) / len(open_t), 1) if open_t else 0,
    }
    from urllib.parse import urlencode
    flt = {"raised_from": raised_from, "raised_to": raised_to, "raised_by": raised_by}
    return templates.TemplateResponse("tickets/list.html", {
        "request": request,
        "current_user": current_user,
        "tickets": tickets,
        "stats": stats,
        "flt": flt,
        "flt_qs": urlencode({k: v for k, v in flt.items() if v}),
        "raiser_opts": raiser_opts,
    })


# ── CONVERT TO TASK ──────────────────────────────────────────────────────────
# Project Task Tracker upload format (2026-11-19, Pankaj's task-upload-template).
TASK_HEADER = ["Subject", "Project", "Type", "Priority", "Module", "Problem", "Solution",
               "Stage", "Location", "Requested by", "Assignee", "Estimated hours",
               "Start date", "Due date"]
TASK_PROJECT = "OxyPc Inventory"
_TASK_TYPE = {"issue": "Bug", "feature": "Feature", "task": "Task", "feedback": "Enhancement"}
_TASK_PRIORITY = {"urgent": "P1 - Immediate", "high": "P2 - High", "normal": "P3 - Normal", "low": "P4 - Low"}
_TASK_STAGE = {"open": "New", "pending": "In Progress", "hold": "In Progress",
               "solved": "Closed", "closed": "Closed"}


_GREETING = re.compile(r"^(hi|hello|hey|dear|good (morning|afternoon|evening))\b[\w\s.,!-]{0,30}$", re.I)


def _task_subject(t: Ticket) -> str:
    """The ticket's subject, else its first real line — greeting lines like
    "Hi Vipin," and a leading "As discussed," are skipped."""
    if (t.subject or "").strip():
        return t.subject.strip()
    text = (t.feedback or "").strip()
    if text.startswith("[From ") and "] " in text:
        text = text.split("] ", 1)[1]
    for line in text.splitlines():
        line = line.strip()
        if not line or _GREETING.match(line):
            continue
        return re.sub(r"^as discussed[,:]?\s*", "", line, flags=re.I).strip() or line
    return "(no subject)"


def ticket_task_row(t: Ticket, names: dict) -> list:
    """One ticket as a Task Tracker row (TASK_HEADER order)."""
    stage = _TASK_STAGE.get((t.status or "").strip().lower(), "New")
    subject = _task_subject(t)
    if len(subject) > 90:
        subject = subject[:87].rstrip() + "..."
    return [
        f"{subject} (#{t.ticket_id})",
        TASK_PROJECT,
        _TASK_TYPE.get((t.ticket_type or "").strip().lower(), "Bug"),
        _TASK_PRIORITY.get((t.priority or "").strip().lower(), "P3 - Normal"),
        (t.group_name or "").strip() or None,
        (t.feedback or "").strip() or None,
        (t.notes or "").strip() or None,
        stage,
        "On Production" if stage == "Closed" else None,
        names.get(t.raised_by) or t.raised_by,
        (names.get(t.assignee) or t.assignee) if t.assignee else None,
        None,
        t.raised_on.date() if t.raised_on else None,
        t.sla_due_at.date() if t.sla_due_at else None,
    ]


@router.get("/convert-to-task")
async def convert_to_task(
    raised_from: str = "",
    raised_to: str = "",
    raised_by: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download the (filtered) tickets in the Task Tracker upload format.
    Admin only, like Close."""
    import io
    from fastapi.responses import Response
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    if current_user.role.value != "admin":
        return JSONResponse({"error": "Only an admin can convert tickets to tasks."}, status_code=403)
    rows = await _filtered_tickets(db, current_user, raised_from, raised_to, raised_by)
    names = await _user_names(db, {t.raised_by for t in rows} | {t.assignee for t in rows})
    wb = Workbook()
    ws = wb.active
    ws.title = "Tasks"
    ws.append(TASK_HEADER)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3864")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for t in rows:
        ws.append(ticket_task_row(t, names))
    for r in ws.iter_rows(min_row=2):
        for c in (r[5], r[6]):
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for c in (r[12], r[13]):
            c.number_format = "dd-mmm-yyyy"
    for col, w in zip("ABCDEFGHIJKLMN", (40, 18, 13, 13, 20, 50, 35, 12, 15, 18, 16, 10, 13, 13)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:N{len(rows) + 1}"
    buf = io.BytesIO()
    wb.save(buf)
    stamp = app_now().strftime("%Y%m%d")
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=tickets_as_tasks_{stamp}.xlsx",
                             "X-Export-Raw": "1"})


# ── RAISE ────────────────────────────────────────────────────────────────────

@router.post("/raise")
async def raise_ticket(
    feedback: str = Form(...),
    subject: str = Form(""),          # 2026-11-14: Raise Ticket has a Subject line
    photos: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not feedback.strip():
        return JSONResponse({"error": "Feedback cannot be empty."}, status_code=400)
    tid = await _next_ticket_id(db)
    # Extra files are ignored rather than rejected: the form already limits the
    # picker to two, so more arriving means a crafted request, not a user error.
    saved = []
    for i, photo in enumerate((photos or [])[:MAX_PHOTOS], start=1):
        path = await _save_photo(photo, tid, i)
        if path:
            saved.append(path)
    ticket = Ticket(
        ticket_id=tid,
        status="open", channel="web", group_name=DEFAULT_GROUP,
        raised_by=current_user.username,
        subject=(subject or "").strip()[:200] or None,
        feedback=feedback.strip(),
        photo1_path=saved[0] if len(saved) > 0 else None,
        photo2_path=saved[1] if len(saved) > 1 else None,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    # Tickets are received and resolved by admin, so every active admin gets
    # a notification (2026-10-14). The raiser is skipped if they are an admin.
    # A notification failure never fails the ticket itself.
    try:
        from services.notifications import create_notification
        from models.user import UserRole
        admins = (await db.execute(
            select(User.id).where(User.role == UserRole.admin, User.status == True,  # noqa: E712
                                  User.id != current_user.id)
        )).scalars().all()
        who = current_user.full_name or current_user.username
        preview = ticket.feedback if len(ticket.feedback) <= 120 else ticket.feedback[:117] + "..."
        for admin_id in admins:
            await create_notification(
                db, user_id=admin_id, title=f"New Ticket {ticket.ticket_id}",
                message=f"{who} raised a ticket: {preview}", notification_type="alert",
            )
    except Exception:
        await db.rollback()
    return JSONResponse({"ok": True, "ticket_id": ticket.ticket_id})


# ── RAISE FROM LOGIN PAGE (no sign-in) ───────────────────────────────────────
# 2026-10-16: "Contact admin for access/issues" on the login page opens a
# ticket form for people who cannot sign in. No session, so:
#   - Raised By is the name the person types (max 50 = column size);
#   - CSRF is the login page's own double-submit cookie (verify_csrf);
#   - rate-limited per IP so the open form can't be used to flood admins.
# The ticket is a normal Ticket row — it appears on /tickets for admins and
# every active admin is notified, exactly like a signed-in ticket.
async def create_public_ticket(db, raised_by, feedback, photos, source="login page", tag="", email=""):
    """A ticket from someone who can't sign in — the staff login page or
    (2026-11-11 PM) the Trade Partner login page. Lands on /tickets for admins
    and notifies them; source goes in the feedback prefix and the notification."""
    # A leading "[...]" is reserved for the partner tag ("[DLR-001] Name") that
    # the partner portal's My Tickets uses to find a partner's tickets, so a
    # typed name can't make a ticket show up in some partner's history.
    name = " ".join((raised_by or "").split()).lstrip("[ ")
    if not name:
        return JSONResponse({"error": "Please enter your name."}, status_code=400)
    name = (tag + name)[:50]
    if not (feedback or "").strip():
        return JSONResponse({"error": "Please describe your issue."}, status_code=400)
    # Email (2026-11-16): where replies and status changes are sent.
    from services.ticket_board import is_valid_email
    email = (email or "").strip()
    if email and not is_valid_email(email):
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)
    tid = await _next_ticket_id(db)
    saved = []
    for i, photo in enumerate((photos or [])[:MAX_PHOTOS], start=1):
        path = await _save_photo(photo, tid, i)
        if path:
            saved.append(path)
    ticket = Ticket(
        ticket_id=tid,
        status="open", channel="web", group_name=DEFAULT_GROUP,
        raised_by=name,
        requester_email=email[:150] or None,
        feedback=f"[From {source}] " + feedback.strip()[:5000],
        photo1_path=saved[0] if len(saved) > 0 else None,
        photo2_path=saved[1] if len(saved) > 1 else None,
    )
    db.add(ticket)
    await db.commit()
    try:
        from services.notifications import create_notification
        from models.user import UserRole
        admins = (await db.execute(
            select(User.id).where(User.role == UserRole.admin, User.status == True)  # noqa: E712
        )).scalars().all()
        body = feedback.strip()
        preview = body if len(body) <= 120 else body[:117] + "..."
        for admin_id in admins:
            await create_notification(
                db, user_id=admin_id, title=f"New Ticket {tid} ({source})",
                message=f"{name} raised a ticket from the {source}: {preview}",
                notification_type="alert",
            )
    except Exception:
        await db.rollback()
    return JSONResponse({"ok": True, "ticket_id": tid})


@router.post("/raise-public")
@limiter.limit("5/hour", key_func=ip_key_func)
async def raise_ticket_public(
    request: Request,
    raised_by: str = Form(...),
    feedback: str = Form(...),
    email: str = Form(""),
    photos: list[UploadFile] = File(default=[]),
    _csrf=Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await create_public_ticket(db, raised_by, feedback, photos, source="login page", email=email)


# ── CONVERSATION for the requester (2026-11-16, Pankaj) ──────────────────────

@router.get("/{ticket_id}/thread")
async def ticket_thread(
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The ticket's conversation as the requester sees it: public replies and
    the activity log, never internal notes. The raiser and admins only."""
    from services.ticket_board import requester_thread
    t = (await db.execute(select(Ticket).where(Ticket.ticket_id == ticket_id))).scalar_one_or_none()
    if not t or (current_user.role.value != "admin" and t.raised_by != current_user.username):
        return JSONResponse({"error": "Ticket not found."}, status_code=404)
    d = await requester_thread(db, t)
    d["can_reply"] = t.raised_by == current_user.username   # admins answer on the Ticket Board
    return JSONResponse(d)


@router.post("/{ticket_id}/reply")
async def ticket_requester_reply(
    ticket_id: str,
    body: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The person who raised the ticket replies in its conversation
    (2026-11-16, Pankaj). Agents reply on the Ticket Board."""
    from services.ticket_board import add_requester_reply, requester_thread
    t = (await db.execute(select(Ticket).where(Ticket.ticket_id == ticket_id))).scalar_one_or_none()
    if not t or t.raised_by != current_user.username:
        return JSONResponse({"error": "Ticket not found."}, status_code=404)
    if not (body or "").strip():
        return JSONResponse({"error": "Write a reply first."}, status_code=400)
    await add_requester_reply(db, t, current_user.username, current_user.full_name or current_user.username, body)
    return JSONResponse({"ok": True})


# ── CLOSE ────────────────────────────────────────────────────────────────────

@router.post("/{ticket_id}/close")
async def close_ticket(
    ticket_id: str,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Close is admin only (2026-11-16, Pankaj); requesters follow the ticket's
    # conversation instead (GET /tickets/{id}/thread).
    if current_user.role.value != "admin":
        return JSONResponse({"error": "Only an admin can close a ticket."}, status_code=403)
    conditions = [Ticket.ticket_id == ticket_id]
    result = await db.execute(select(Ticket).where(*conditions))
    ticket = result.scalar_one_or_none()
    if not ticket:
        return JSONResponse({"error": "Ticket not found."}, status_code=404)
    if ticket.simple_status == "Closed":
        return JSONResponse({"error": "Already closed."}, status_code=400)
    # Close = Solved on the Ticket Board (2026-11-13), logged in its activity.
    from services.ticket_board import set_status, add_event
    set_status(db, ticket, "solved", current_user)
    if notes.strip():
        add_event(db, ticket, current_user, "Closed from Tickets Raised — " + notes.strip()[:500])
    # Closing notes are appended, never overwritten — an earlier note is part of
    # the ticket's history and a later closer should not silently erase it.
    if notes.strip():
        ticket.notes = (ticket.notes + "\n" + notes.strip()) if ticket.notes else notes.strip()
    ticket.updated_at = app_now()
    await db.commit()

    # Tell the person who raised the ticket (and only them) when someone else —
    # an admin — closes it (2026-10-15). Personal alert: user_id is the raiser,
    # so nobody else's Alerts list shows it. Never fails the close itself.
    if ticket.raised_by and ticket.raised_by != current_user.username:
        try:
            raiser_id = (await db.execute(
                select(User.id).where(User.username == ticket.raised_by)
            )).scalar_one_or_none()
            if raiser_id:
                from services.notifications import create_notification
                who = current_user.full_name or current_user.username
                msg = f"{who} closed your ticket."
                if notes.strip():
                    note = notes.strip()
                    msg += " Notes: " + (note if len(note) <= 160 else note[:157] + "...")
                await create_notification(
                    db, user_id=raiser_id, title=f"Ticket {ticket.ticket_id} Closed",
                    message=msg, notification_type="success",
                )
        except Exception:
            await db.rollback()
    return JSONResponse({"ok": True})
