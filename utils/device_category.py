"""Device Type <-> Sub-Category pairing (2026-10-22).

Tags carry both a Device Type ("TFT Monitor") and a Sub-Category ("TFT"),
from different master lists. Uploads often fill only one, which left ~6,000
tags with a blank Device Type. When one is blank it is filled from the other:
matched against that field's master list ignoring case, then a few known
spellings, else copied as typed. A value that IS given is never changed.
"""

# Spellings seen in the data that aren't in the master lists as written.
_TO_DEVICE_TYPE = {"tft": "TFT Monitor", "sarvar": "Server", "all in one": "All-in-One", "aio": "All-in-One"}
_TO_SUB_CATEGORY = {"all-in-one": "All In One", "tft": "TFT Monitor", "sarvar": "Server", "aio": "All In One"}


def _match(value, master, aliases):
    v = (value or "").strip()
    if not v:
        return None
    by_lower = {m.lower(): m for m in (master or [])}
    return by_lower.get(v.lower()) or aliases.get(v.lower())


def device_type_for(sub_category, device_type_master) -> str | None:
    """Device Type for a Sub-Category, or None if it matches nothing known."""
    return _match(sub_category, device_type_master, _TO_DEVICE_TYPE)


def pair_type_and_category(device_type, sub_category, device_type_master, sub_category_master):
    """Return (device_type, sub_category) with a blank one filled from the other.
    Unknown values are copied as typed; lengths fit the Device columns
    (device_type 30, sub_category 20)."""
    dt = (device_type or "").strip() or None
    sc = (sub_category or "").strip() or None
    if not dt and sc:
        dt = _match(sc, device_type_master, _TO_DEVICE_TYPE) or sc
    if not sc and dt:
        sc = _match(dt, sub_category_master, _TO_SUB_CATEGORY) or dt
    return (dt[:30] if dt else None), (sc[:20] if sc else None)
