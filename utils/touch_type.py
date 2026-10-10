"""Touch Type (Touch / Non-Touch) display helper.

Device.touch_type is set by the Final QC form ("touch" / "non_touch"). Blank
means it was never set and is shown as NT (Non-Touch), the default.
"""


def touch_badge_html(touch_type) -> str:
    """Small T / NT badge shown next to a Tag Number in server-built table rows."""
    if (touch_type or "").strip().lower() == "touch":
        return '<span class="badge bg-primary ms-1" title="Touch">T</span>'
    return '<span class="badge bg-secondary ms-1" title="Non-Touch">NT</span>'
