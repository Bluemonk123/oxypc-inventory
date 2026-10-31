"""The 12 device categories (2026-10-30, Pankaj): Dashboard category tiles,
Device Type tidy-up (Admin -> Data Fixes) and Master Data "Device Form
Factors" all use this one list."""

DEVICE_TYPES_12 = ["Laptop", "Desktop", "TFT Monitor", "Tablet", "Mini PC", "Server", "Router",
                   "Printer", "Switch", "Projector", "All-in-One", "Mobile"]

# Other spellings seen on tags -> the category they belong to.
DEVICE_TYPE_ALIASES = {"tiny": "Mini PC", "thinclient": "Mini PC", "thin client": "Mini PC",
                       "mobile phone": "Mobile", "mobile phones": "Mobile", "tft": "TFT Monitor",
                       "monitor": "TFT Monitor", "all in one": "All-in-One", "aio": "All-in-One"}

_BY_LOWER = {n.lower(): n for n in DEVICE_TYPES_12}


def canonical_device_type(value):
    """The category a Device Type value belongs to (exact name, any case, or a
    known alias), else None."""
    v = (value or "").strip().lower()
    if not v:
        return None
    return _BY_LOWER.get(v) or DEVICE_TYPE_ALIASES.get(v)
