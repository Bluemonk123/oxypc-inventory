"""Move to External Partner (2026-11-11, Pankaj).

Bulk action on the Accounts List. The selected Accounts become External
Partners (a Dealer row linked to the Account, the same bridge Partner Accounts →
Generate Access uses) WITHOUT a portal login — the login is created later from
Partner Accounts → Generate Access.

The modal picks the owner at one level — Executive or Team Leader. That person
becomes the partner's Sales Owner, and the levels above (Team Leader, Manager,
AVP/VP) are filled from their Report To chain by designation
(services/crm_team.designation_hierarchy), editable before saving.
Those levels are written to the Account's team mapping; levels below the
picked one, and Finance, are left as they are.
"""
from sqlalchemy import select

from services.crm_team import HIERARCHY


def levels_from(owner_level: str, owner_id, picks: dict) -> dict:
    """{bucket: [user_id]} for the owner's level and every level above it that
    has someone picked. picks = {bucket: user_id or None} for the levels above."""
    start = HIERARCHY.index(owner_level)
    out = {owner_level: [owner_id]}
    for b in HIERARCHY[start + 1:]:
        if picks.get(b):
            out[b] = [picks[b]]
    return out


async def move_to_partner(db, contacts, user, *, owner, owner_level, picks,
                          partner_type, price_segment, request=None) -> dict:
    """Make each Account an External Partner (no login) and, when an owner is
    picked, map its team (2026-11-11 PM: team mapping is optional).

    owner picked   → Sales Owner + Account Manager = owner; Executive / Team
                     Leader / Manager / AVP/VP replaced by owner + picks.
    no owner       → team left as it is; Sales Owner stays what the partner
                     already had, else the Account's Account Manager.
    Returns {"moved": [names], "updated": [names]} — updated = already a partner."""
    from routers.partner_admin import _dealer_for_account
    from services.audit_engine import audit
    from services.crm_team import replace_team_buckets, HIERARCHY
    from models.crm_team import CRMContactTeamMember

    buckets = {}
    if owner:
        team = levels_from(owner_level, owner.id, picks)
        buckets = {b: team.get(b, []) for b in HIERARCHY}       # the whole team, as in Map Team
    moved, updated = [], []
    for c in contacts:
        label = c.company_name or c.contact_code
        dealer, created = await _dealer_for_account(db, c, user.username)
        was_partner = (not created) and (dealer.crm_contact_id == c.id or dealer.portal_enabled)
        old = {"sales_owner": dealer.sales_owner_username, "partner_type": dealer.partner_type,
               "price_segment": dealer.price_segment, "account_manager": c.assigned_to}
        old_team = {}
        if buckets:
            for r in (await db.execute(select(CRMContactTeamMember).where(
                    CRMContactTeamMember.contact_id == c.id,
                    CRMContactTeamMember.role_bucket.in_(list(buckets))))).scalars().all():
                old_team.setdefault(r.role_bucket, []).append(str(r.user_id))
        dealer.crm_contact_id = c.id
        # None = not chosen: keep the partner's own, else the defaults
        dealer.partner_type = partner_type or dealer.partner_type or "dealer"
        dealer.price_segment = price_segment or dealer.price_segment or "new_dealer"
        # portal_enabled / phone / password are left alone: no login is created here
        if owner:
            dealer.sales_owner_username = owner.username
            c.assigned_to = owner.username                       # Account Manager
            await replace_team_buckets(db, contact_id=c.id, bucket_user_ids=buckets, created_by=user.username)
        elif not dealer.sales_owner_username and c.assigned_to:
            dealer.sales_owner_username = c.assigned_to
        new = {"crm_contact_id": str(c.id), "sales_owner": dealer.sales_owner_username,
               "partner_type": dealer.partner_type, "price_segment": dealer.price_segment, "dealer_created": created}
        if owner:
            new.update({"owner_level": owner_level, "account_manager": owner.username,
                        "team": {b: [str(x) for x in v] for b, v in buckets.items()}})
        await audit(db, action="ACCOUNT_MOVED_TO_PARTNER", user=user, table_name="dealers",
                    record_id=str(dealer.id), old_value={**old, "team": old_team} if owner else old,
                    new_value=new, request=request)
        (updated if was_partner else moved).append(label)
    return {"moved": moved, "updated": updated}
