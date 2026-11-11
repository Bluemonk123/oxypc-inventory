"""
Account Team Mapping — shared logic used by both the Map Team modal's save
route and the CSV bulk-upload route in routers/crm_contacts.py.
"""
import re
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.crm_team import CRMContactTeamMember

# "finance" (2026-11-09, Pankaj): the Finance team member for the account.
# Levels (2026-11-11 PM, Pankaj): Executive, Team Leader, Manager, AVP/VP, then
# Finance. Team Leader is the old AM/DM/GM slot: the key stays am_dm_gm so every
# existing mapping (and the credit check that uses it) carries over unchanged.
ROLE_BUCKETS = ["executive", "am_dm_gm", "manager", "vp_avp", "finance"]

BUCKET_LABELS = {
    "executive": "Executive",
    "am_dm_gm": "Team Leader",
    "manager": "Manager",
    "vp_avp": "AVP/VP",
    "finance": "Finance Manager",
}

# Team Mapping CSV column per level; the old key is still accepted on upload.
CSV_COLUMNS = {"executive": "executive", "am_dm_gm": "team_leader", "manager": "manager",
               "vp_avp": "avp_vp", "finance": "finance"}

# The levels above each one, in order — Report To fills them in (Move to External Partner).
HIERARCHY = ["executive", "am_dm_gm", "manager", "vp_avp"]

BUCKET_KEYWORDS = {
    "vp_avp": ["VP", "AVP"],
    "manager": ["Manager"],
    "am_dm_gm": ["Team Leader", "Team Lead", "TL"],   # 2026-11-11 PM: was DM / GM / CTO / CEO
    "executive": ["Executive"],
    # Finance team: designation (or role name) with the whole word Finance or Accounts.
    "finance": ["Finance", "Accounts"],
}


def designation_level(u):
    """Which upper level a user's designation (or role) belongs to: vp_avp,
    manager or am_dm_gm (Team Leader), checked in that order; None otherwise."""
    role_val = u.role.value if hasattr(u.role, "value") else str(u.role or "")
    for b in ("vp_avp", "manager", "am_dm_gm"):
        if _matches_bucket(u.designation, b) or _matches_bucket(role_val, b):
            return b
    return None


def designation_hierarchy(users) -> dict:
    """{user_id: {level: user_id}} — walking up each user's Report To, the first
    person whose designation is Team Leader / Manager / AVP/VP fills that level
    (2026-11-11 PM, Pankaj). People in between with other designations are
    skipped. Stops at a loop or a user who isn't in the list (inactive)."""
    by_name = {u.username: u for u in users}
    out = {}
    for u in users:
        found, seen, cur = {}, {u.username}, u
        while cur.reports_to and cur.reports_to in by_name and cur.reports_to not in seen:
            cur = by_name[cur.reports_to]
            seen.add(cur.username)
            lvl = designation_level(cur)
            if lvl and lvl not in found:
                found[lvl] = str(cur.id)
        out[str(u.id)] = found
    return out


def team_from_owner(owner_level: str, owner_id, hierarchy: dict, overrides: dict = None) -> dict:
    """{bucket: [user_id]} for an Executive / Team Leader owner: the owner's own
    level plus every level above it — from overrides when given (a value of ''
    means "leave empty"), otherwise from the designation hierarchy."""
    overrides = overrides or {}
    up = hierarchy.get(str(owner_id), {})
    out = {owner_level: [owner_id]}
    for b in HIERARCHY[HIERARCHY.index(owner_level) + 1:]:
        v = overrides[b] if b in overrides else up.get(b)
        if v:
            out[b] = [v]
    return out


def _matches_bucket(text, bucket: str) -> bool:
    if not text:
        return False
    for kw in BUCKET_KEYWORDS[bucket]:
        if re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
            return True
    return False


def eligible_users_for_bucket(users, bucket: str) -> list:
    """Users whose designation or role name contains a whole-word match for
    this bucket's keywords — never a raw substring match, so 'TEAM' or
    'Samir' doesn't false-positive on the 'AM' keyword."""
    if bucket not in ROLE_BUCKETS:
        raise ValueError(f"Unknown role bucket: {bucket}")
    result = []
    for u in users:
        role_val = u.role.value if hasattr(u.role, "value") else str(u.role)
        if _matches_bucket(u.designation, bucket) or _matches_bucket(role_val, bucket):
            result.append(u)
    return result


async def replace_team_buckets(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID,
    bucket_user_ids: dict,
    created_by: str,
) -> None:
    """Replace mapping rows for the given (contact, bucket) pairs.

    Only buckets present as a key in bucket_user_ids are touched — a bucket
    left out of the dict entirely is left alone (this is what makes bulk
    mode's "only replace buckets the admin actually selected someone in"
    semantics work: the caller builds the dict with only the touched keys).
    A bucket present with an empty list clears that bucket — this is what
    makes single-company mode's "blank means clear" semantics work: the
    caller always includes every bucket key.
    """
    for bucket, user_ids in bucket_user_ids.items():
        await db.execute(
            delete(CRMContactTeamMember).where(
                CRMContactTeamMember.contact_id == contact_id,
                CRMContactTeamMember.role_bucket == bucket,
            )
        )
        for uid in user_ids:
            db.add(CRMContactTeamMember(
                contact_id=contact_id, user_id=uid, role_bucket=bucket,
                created_by=created_by,
            ))


# Team Mapping section on the Supplier / Customer Deal pages (2026-11-10,
# Pankaj): the deal's Account team in this order; Finance Manager is shown
# only in the Map Team modal, not here.
DEAL_TEAM_BUCKETS = [(b, BUCKET_LABELS[b]) for b in HIERARCHY]


async def bucket_options(db, users) -> dict:
    """{bucket: [(id, name)]}: users whose designation fits the level, plus anyone
    already mapped on that level — so renaming a level's keywords never drops a
    mapped person from the dropdown (and from the mapping on the next save)."""
    from sqlalchemy import select
    mapped = {}
    for b, uid in (await db.execute(select(CRMContactTeamMember.role_bucket, CRMContactTeamMember.user_id)
                                    .distinct())).all():
        mapped.setdefault(b, set()).add(uid)
    out = {}
    for b in ROLE_BUCKETS:
        keep = {u.id for u in eligible_users_for_bucket(users, b)} | mapped.get(b, set())
        out[b] = [(str(u.id), u.full_name) for u in users if u.id in keep]
    return out


async def deal_team(db, contact_id) -> list:
    """[(label, [full names])] for DEAL_TEAM_BUCKETS of the Account."""
    from sqlalchemy import select
    from models.user import User
    names = {}
    if contact_id:
        for bucket, name in (await db.execute(
                select(CRMContactTeamMember.role_bucket, User.full_name)
                .join(User, CRMContactTeamMember.user_id == User.id)
                .where(CRMContactTeamMember.contact_id == contact_id))).all():
            names.setdefault(bucket, []).append(name)
    return [(label, names.get(b, [])) for b, label in DEAL_TEAM_BUCKETS]


async def team_modal_context(db, contact_id) -> dict:
    """Map Team modal on a deal page (2026-11-11): bucket options (active users
    per bucket) and the Account's current mapping as {bucket: "id,id"}."""
    from sqlalchemy import select
    from models.user import User
    users = (await db.execute(select(User).where(User.status == True).order_by(User.full_name))).scalars().all()  # noqa: E712
    options = await bucket_options(db, users)
    sel = {}
    if contact_id:
        for bucket, uid in (await db.execute(
                select(CRMContactTeamMember.role_bucket, CRMContactTeamMember.user_id)
                .where(CRMContactTeamMember.contact_id == contact_id))).all():
            sel.setdefault(bucket, []).append(str(uid))
    return {"bucket_options": options, "mt_selected": {b: ",".join(v) for b, v in sel.items()},
            "mt_contact_id": str(contact_id) if contact_id else "",
            # Map Team picks one Executive / Team Leader and fills the levels above
            # from Report To (2026-11-11 PM): every active user + each one's chain.
            "pm_users": [(str(u.id), u.full_name or u.username, u.designation or "") for u in users],
            "pm_levels": designation_hierarchy(users)}
