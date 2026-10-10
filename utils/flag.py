"""Urgent flag (Inventory Manager → "Flag Items") display helpers.

A device is flagged while Device.flag_authorized_by / flag_reason is set; the
flag clears itself when the tag moves back into Stock In (models/device.py).
"""
from html import escape


def is_flagged(device) -> bool:
    return bool(getattr(device, "flagged_at", None) or getattr(device, "flag_reason", None))


def flag_tooltip(device) -> str:
    def d(v):
        return v.strftime("%d-%m-%Y") if v else "—"
    return (f"Urgent flag — Authorized by: {device.flag_authorized_by or '—'} | "
            f"Requested: {d(device.flag_request_date)} | Target: {d(device.flag_target_date)} | "
            f"Reason: {device.flag_reason or '—'}")


def flag_badge_html(device) -> str:
    """Red flag icon for a Tag Number cell (hover = details); empty string when not flagged."""
    if not is_flagged(device):
        return ""
    return (f'<i class="bi bi-flag-fill text-danger ms-1" style="cursor:help" '
            f'title="{escape(flag_tooltip(device))}" aria-label="Flagged"></i>')
