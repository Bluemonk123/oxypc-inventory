"""Account cells shared with the Accounts list (2026-11-11, Pankaj): Place
(Locations count -> Locations pop-up), Person (Contacts count -> Contacts
pop-up, grouped per location) and Team (mapped team count -> Team pop-up),
in the same data shape routers/crm_contacts.py builds for /crm/contacts, so
the Activity Board's Inspection / Screening cards show them the same way.
Pop-ups + script: templates/crm/contacts/_account_cell_modals.html."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from models.crm import CRMContactLocation
from models.crm_team import CRMContactTeamMember
from models.user import User, ROLE_LABELS
from services.crm_team import ROLE_BUCKETS


async def account_cell_maps(db, contact_ids):
    """(locations_map, numbers_map, team_map), keyed by str(contact_id)."""
    ids = [c for c in contact_ids if c]
    locations_map, numbers_map, team_map = {}, {}, {}
    if not ids:
        return locations_map, numbers_map, team_map
    for member, full_name, designation, role in (await db.execute(
            select(CRMContactTeamMember, User.full_name, User.designation, User.role)
            .join(User, CRMContactTeamMember.user_id == User.id)
            .where(CRMContactTeamMember.contact_id.in_(ids)))).all():
        t = team_map.setdefault(str(member.contact_id), {b: [] for b in ROLE_BUCKETS})
        t.setdefault(member.role_bucket, []).append({
            "id": str(member.user_id), "name": full_name, "designation": designation or "—",
            "role": ROLE_LABELS.get(role, role)})
    for loc in (await db.execute(
            select(CRMContactLocation).where(CRMContactLocation.contact_id.in_(ids))
            .options(selectinload(CRMContactLocation.contact_numbers))
            .order_by(CRMContactLocation.contact_id, CRMContactLocation.sort_order))).scalars().all():
        cid = str(loc.contact_id)
        locations_map.setdefault(cid, []).append({"address": loc.address or "", "city": loc.city or "",
                                                  "state": loc.state or ""})
        numbers_map.setdefault(cid, []).append({
            "location": ", ".join(filter(None, [loc.address, loc.city, loc.state])) or "Location",
            "numbers": [{"role": n.person_role or "", "name": n.person_name or "", "phone": n.phone or "",
                         "email": n.email or ""} for n in loc.contact_numbers]})
    return locations_map, numbers_map, team_map
