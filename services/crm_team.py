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
# Order (2026-11-11, Pankaj): Manager, Executive/Sr Executive, VP/AVP,
# DM/GM/CTO/CEO (was AM/DM/GM; key kept as am_dm_gm so existing mappings stay),
# then Finance Manager.
ROLE_BUCKETS = ["manager", "executive", "vp_avp", "am_dm_gm", "finance"]

BUCKET_LABELS = {
    "manager": "Manager",
    "executive": "Executive/Sr Executive",
    "vp_avp": "VP/AVP",
    "am_dm_gm": "DM/GM/CTO/CEO",
    "finance": "Finance Manager",
}

BUCKET_KEYWORDS = {
    "vp_avp": ["VP", "AVP"],
    "manager": ["Manager"],
    "am_dm_gm": ["DM", "GM", "CTO", "CEO"],   # 2026-11-11: was AM / DM / GM
    "executive": ["Executive"],
    # Finance team: designation (or role name) with the whole word Finance or Accounts.
    "finance": ["Finance", "Accounts"],
}


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
DEAL_TEAM_BUCKETS = [("manager", "Manager"), ("executive", "Executive/Sr Executive"),
                     ("vp_avp", "VP/AVP"), ("am_dm_gm", "DM/GM/CTO/CEO")]


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
    options = {b: [(str(u.id), u.full_name) for u in eligible_users_for_bucket(users, b)] for b in ROLE_BUCKETS}
    sel = {}
    if contact_id:
        for bucket, uid in (await db.execute(
                select(CRMContactTeamMember.role_bucket, CRMContactTeamMember.user_id)
                .where(CRMContactTeamMember.contact_id == contact_id))).all():
            sel.setdefault(bucket, []).append(str(uid))
    return {"bucket_options": options, "mt_selected": {b: ",".join(v) for b, v in sel.items()},
            "mt_contact_id": str(contact_id) if contact_id else ""}
