"""Ticket raising system — users log feedback, can see own tickets."""
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User
from models.ticket import Ticket
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


# Only real image types are accepted. A ticket attachment is displayed straight
# back to an admin, so anything that could be served as active content is refused
# rather than sanitised — the feature does not need that flexibility.
ALLOWED_PHOTO_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_PHOTOS = 2
MAX_PHOTO_BYTES = 5 * 1024 * 1024


async def _save_photo(photo: UploadFile, ticket_id: str, idx: int) -> str | None:
    """Persist one attachment and return its /uploads path, or None if unusable."""
    if not photo or not photo.filename:
        return None
    if photo.content_type not in ALLOWED_PHOTO_TYPES:
        return None
    upload_dir = Path("uploads/tickets")
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Extension comes from the vetted content type, never from the user-supplied
    # filename — that string is attacker-controlled and ends up on disk.
    ext = {"image/png": "png", "image/jpeg": "jpg",
           "image/gif": "gif", "image/webp": "webp"}[photo.content_type]
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


def _ticket_ctx(t: Ticket) -> dict:
    age_label, age_cls = _age_pill(t.raised_on)
    return {
        "ticket_id":  t.ticket_id,
        "status":     t.status,
        "raised_on":  t.raised_on.strftime("%d-%m-%Y %H:%M") if t.raised_on else "—",
        "raised_by":  t.raised_by,
        "feedback":   t.feedback or "",
        "notes":      t.notes or "",
        "photos":     [p for p in (t.photo1_path, t.photo2_path) if p],
        "age_label":  age_label,
        "age_cls":    age_cls,
    }


# ── LIST ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_tickets(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Ticket).order_by(Ticket.raised_on.desc())
    if current_user.role.value != "admin":
        query = query.where(Ticket.raised_by == current_user.username)
    result = await db.execute(query)
    tickets = [_ticket_ctx(t) for t in result.scalars().all()]
    return templates.TemplateResponse("tickets/list.html", {
        "request": request,
        "current_user": current_user,
        "tickets": tickets,
    })


# ── RAISE ────────────────────────────────────────────────────────────────────

@router.post("/raise")
async def raise_ticket(
    feedback: str = Form(...),
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
        raised_by=current_user.username,
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


# ── CLOSE ────────────────────────────────────────────────────────────────────

@router.post("/{ticket_id}/close")
async def close_ticket(
    ticket_id: str,
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conditions = [Ticket.ticket_id == ticket_id]
    if current_user.role.value != "admin":
        conditions.append(Ticket.raised_by == current_user.username)
    result = await db.execute(select(Ticket).where(*conditions))
    ticket = result.scalar_one_or_none()
    if not ticket:
        return JSONResponse({"error": "Ticket not found."}, status_code=404)
    if ticket.status == "Closed":
        return JSONResponse({"error": "Already closed."}, status_code=400)
    ticket.status = "Closed"
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
