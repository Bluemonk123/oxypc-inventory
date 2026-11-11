"""Report To hierarchy (2026-11-10): a user's managers, all the way up
(users.reports_to). Used for Telecalling Quotation Level 1 approval: the
creator's Report To, their Report To, and so on, may approve."""
from sqlalchemy import select

from models.user import User


async def managers_chain(db, username: str | None, max_depth: int = 15) -> list[str]:
    chain, seen, cur = [], {username}, username
    for _ in range(max_depth):
        if not cur:
            break
        nxt = (await db.execute(select(User.reports_to).where(User.username == cur))).scalar_one_or_none()
        if not nxt or nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    return chain


async def is_in_chain(db, approver: str, creator: str | None) -> bool:
    return bool(approver) and approver in await managers_chain(db, creator)


def _role(u) -> str:
    return u.role.value if hasattr(u.role, "value") else str(u.role)


async def can_approve(db, user, creator: str | None) -> bool:
    """Admin, or anyone above the creator in the Report To chain."""
    return _role(user) == "admin" or await is_in_chain(db, user.username, creator)


async def subordinates(db, username: str) -> set[str]:
    """Everyone below `username` in Report To (direct and indirect)."""
    rows = (await db.execute(select(User.username, User.reports_to))).all()
    kids = {}
    for u, r in rows:
        if r:
            kids.setdefault(r, []).append(u)
    out, stack = set(), [username]
    while stack:
        for k in kids.get(stack.pop(), []):
            if k not in out and k != username:
                out.add(k)
                stack.append(k)
    return out
