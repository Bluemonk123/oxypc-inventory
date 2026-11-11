"""Locations and Location Contacts from a CSV row (2026-11-11, Pankaj).

Shared by the two account uploads in routers/crm_contacts.py:
  * Bulk Upload Accounts — creates / reuses Accounts and their Locations.
  * Upload Locations — adds Locations and Location Contacts to Accounts that
    already exist (matched by gstin, then company_name); never creates one.

A Location is reused (not duplicated) when the Account already has one with the
same address + city + state. A Location Contact is reused when that Location
already has a number with the same phone (or, without a phone, the same name).
"""
from sqlalchemy import select

LOCATION_COLS = ("location_address", "location_city", "location_state", "location_email")
CONTACT_COLS = ("location_contact_role", "location_contact_name", "location_contact_phone", "location_contact_email")


def cell(row, col) -> str:
    return (row.get(col) or "").strip()


class LocationWriter:
    """Caches each Account's Locations and each Location's numbers for one file."""

    def __init__(self, db):
        self.db = db
        self._locs = {}      # contact_id -> {(addr, city, state) lower: CRMContactLocation}
        self._nums = {}      # location_id -> {phone or name lower}
        self.locations_created = 0
        self.contacts_created = 0

    async def _locations_for(self, contact) -> dict:
        from models.crm import CRMContactLocation
        cache = self._locs.get(contact.id)
        if cache is None:
            existing = (await self.db.execute(select(CRMContactLocation).where(
                CRMContactLocation.contact_id == contact.id))).scalars().all()
            cache = {((l.address or "").strip().lower(), (l.city or "").strip().lower(),
                      (l.state or "").strip().lower()): l for l in existing}
            self._locs[contact.id] = cache
        return cache

    async def _numbers_for(self, location) -> set:
        from models.crm import CRMContactNumber
        cache = self._nums.get(location.id)
        if cache is None:
            existing = (await self.db.execute(select(CRMContactNumber).where(
                CRMContactNumber.location_id == location.id))).scalars().all()
            cache = {((n.phone or "").strip() or (n.person_name or "").strip().lower()) for n in existing}
            self._nums[location.id] = cache
        return cache

    async def location(self, contact, row):
        """The row's Location under contact (created if new), or None when the
        row has no location_address / city / state. Returns (location, created)."""
        from models.crm import CRMContactLocation
        addr, city, state, email = (cell(row, c) for c in LOCATION_COLS)
        if not (addr or city or state):
            return None, False
        cache = await self._locations_for(contact)
        key = (addr.lower(), city.lower(), state.lower())
        loc = cache.get(key)
        if loc is not None:
            return loc, False
        loc = CRMContactLocation(contact_id=contact.id, contact_email=email or None, address=addr or None,
                                 city=city or None, state=state or None, sort_order=len(cache))
        self.db.add(loc)
        await self.db.flush()
        cache[key] = loc
        self.locations_created += 1
        return loc, True

    async def contact_person(self, contact, location, row) -> bool:
        """Add the row's Location Contact under location. True if added."""
        from models.crm import CRMContactNumber
        role, name, phone, email = (cell(row, c) for c in CONTACT_COLS)
        if not (name or phone):
            return False
        cache = await self._numbers_for(location)
        key = phone or name.lower()
        if key in cache:
            return False
        # contact_id is model-docs "legacy", but the live DB still enforces NOT NULL on it.
        self.db.add(CRMContactNumber(contact_id=contact.id, location_id=location.id,
                                     person_role=role or None, person_name=name or None,
                                     phone=phone or None, email=email or None, sort_order=len(cache)))
        cache.add(key)
        self.contacts_created += 1
        # The Account's own Company Details, if still empty, take the first contact
        if not contact.contact_person and name:
            contact.contact_person = name
        if not contact.phone and phone:
            contact.phone = phone
            contact.whatsapp = contact.whatsapp or phone
        return True
