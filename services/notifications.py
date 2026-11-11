"""
Notification service — create notifications from anywhere without circular imports.

Who gets a personal notification (2026-11-09, Pankaj):
  * the Assignee — the user it is for (assigned to / moved to / waiting on them)
  * the Assigner — the user who did the action (assigned / moved it) — a copy
  * the Assignee's "Report To" manager (User Management) — a copy
  * the Assigner's "Report To" manager — a copy
Each person gets it once, even when one person is several of these.
Each of these rows is also emailed (services/email_notify.py). Broadcasts
(user_id=None) stay in-app only.
"""
import uuid as _uuid
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models.notification import Notification


async def create_notification(
    db: AsyncSession,
    *,
    user_id: Optional[_uuid.UUID] = None,
    title: str,
    message: str,
    notification_type: str = "info",
    barcode: Optional[str] = None,
    brand: Optional[str] = None,
    model: Optional[str] = None,
    stage: Optional[str] = None,
    copies: bool = True,
) -> None:
    """
    Insert a notification row (and its copies).

    - user_id=None  → broadcast (visible to ALL users, in-app only).
    - user_id=<id>  → personal notification for that user, plus a copy for
                      the user doing the action and for the recipient's
                      Report To manager (copies=False sends only the first).
    - notification_type: info | success | warning | alert
    """
    def _row(uid, t, m):
        return Notification(user_id=uid, title=t, message=m, notification_type=notification_type,
                            barcode=barcode, brand=brand, model=model, stage=stage)

    db.add(_row(user_id, title, message))
    if user_id is not None and copies:
        try:
            await _add_copies(db, user_id, title, message, _row)
        except Exception:
            pass   # a copy must never stop the main notification
    await db.commit()


async def _add_copies(db: AsyncSession, user_id, title: str, message: str, _row) -> None:
    """Copies for the person who did the action and the recipient's manager."""
    from models.user import User
    try:
        from services.repair_location import current_user_id   # set per request at login
        actor_id = current_user_id.get()
    except Exception:
        actor_id = None
    recipient = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if recipient is None:
        return
    to_name = recipient.full_name or recipient.username
    # "Device Assigned to You" reads wrong on a copy -> "Device Assigned to Ravi".
    if " to You" in title:
        copy_title = title.replace(" to You", f" to {to_name}")
    else:
        copy_title = f"{title} — {to_name}"
    sent = {str(user_id)}
    # One copy per person per action: an action that notifies several users
    # (e.g. "Tag picked by another engineer") must not send the actor one
    # copy for each of them. Remembered on the session for this request.
    done = db.info.setdefault("oxypc_notif_copies", set())
    key = (title, message)
    if actor_id and str(actor_id) not in sent and (str(actor_id), key) not in done:
        sent.add(str(actor_id))
        done.add((str(actor_id), key))
        db.add(_row(actor_id, copy_title,
                    f"{message}\n(Copy: sent to {to_name}; you did this action.)"))
    async def _manager(person):
        if not person or not person.reports_to:
            return None
        return (await db.execute(select(User).where(
            User.username == person.reports_to, User.status == True))).scalar_one_or_none()  # noqa: E712

    actor = None
    if actor_id:
        actor = (await db.execute(select(User).where(User.id == actor_id))).scalar_one_or_none()
    actor_name = (actor.full_name or actor.username) if actor else ""
    for mgr, why in ((await _manager(recipient), f"sent to {to_name}, who reports to you"),
                     (await _manager(actor), f"{actor_name} did this action and reports to you; sent to {to_name}")):
        if mgr and str(mgr.id) not in sent and (str(mgr.id), key) not in done:
            sent.add(str(mgr.id))
            done.add((str(mgr.id), key))
            db.add(_row(mgr.id, copy_title, f"{message}\n(Copy: {why}.)"))
