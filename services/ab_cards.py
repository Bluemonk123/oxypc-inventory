"""Activity Board cards — the one list of cards (2026-11-15, Pankaj).

This list drives BOTH the Activity Board layout and Master Data -> Role
Additional Permissions -> "Activity Board Cards". Add a card here (and its
markup in templates/activity_board/index.html) and it appears on the
permission page; remove it here and it disappears there — nothing else to edit.

Layout (group):
  tabs  — one tab bar: Account Verification, Pricing Approvals, Supplier Deals,
          Customer Deals, Parts Sourcing, Device Sourcing
  row   — Negotiation and Quotations side by side below the tabs (a hidden one
          gives the other the full width)
  stage — Inspection and Screening side by side at the bottom (same rule)

Per role: app_settings "ab_cards_<role>" = {"cards": [ticked], "known": [cards
that existed when it was saved]} (no schema change; an older plain list counts
as "cards" with every current card known). Admin always sees every card. A role never saved keeps the old
behaviour (default_cards). A card added later is not in "known", so roles saved
before it get its default until an admin saves them again. A ticked card still
shows only what its own rule allows (deal / approval cards: items assigned to
the user); Parts / Device Sourcing ticked = the role can Verify / Reject there.
"""
import json

from sqlalchemy import select

from models.settings import AppSetting

GROUPS = [   # key, label on the permission page
    ("tabs", "Tabs"),
    ("row", "Approval cards (below the tabs)"),
    ("stage", "Stage cards (bottom)"),
]
AB_CARDS = [   # key, label, note, group — page order
    ("accounts", "Account Verification", "Accounts with Manager and Credit to verify", "tabs"),
    ("pricing", "Pricing Approvals", "Pricing approvals assigned to the user", "tabs"),
    ("supplier", "Supplier Deals", "Inspection assigned to the user", "tabs"),
    ("customer", "Customer Deals", "Screening assigned to the user", "tabs"),
    ("partsrc", "Parts Sourcing", "Verify / Reject parts sourcing requests", "tabs"),
    ("devsrc", "Device Sourcing", "Verify / Reject device sourcing requests", "tabs"),
    ("negotiation", "Negotiation", "Negotiation approvals assigned to the user", "row"),
    ("quotations", "Quotations", "Quotation approvals assigned to the user", "row"),
    ("inspection", "Inspection", "Inspection assigned to the user", "stage"),
    ("screening", "Screening", "Screening assigned to the user", "stage"),
]
AB_KEYS = [c[0] for c in AB_CARDS]
TAB_CARDS = tuple(c[0] for c in AB_CARDS if c[3] == "tabs")
ROW_CARDS = tuple(c[0] for c in AB_CARDS if c[3] == "row")
STAGE_CARDS = tuple(c[0] for c in AB_CARDS if c[3] == "stage")
SOURCING = ("partsrc", "devsrc")
PREFIX = "ab_cards_"


def grouped():
    """[(group label, [(key, label, note)])] for the permission page."""
    return [(gl, [(k, l, n) for k, l, n, g in AB_CARDS if g == gk]) for gk, gl in GROUPS
            if any(c[3] == gk for c in AB_CARDS)]


def default_cards(role):
    """What the role saw before this setting existed: every card except Parts /
    Device Sourcing, which were only for admin and Activity Board -> Edit."""
    from models.role_permissions import has_explicit_perm, has_perm
    edit = role == "admin" or (has_explicit_perm(role, "activity_board") and has_perm(role, "activity_board", "edit"))
    return set(AB_KEYS) if edit else set(AB_KEYS) - set(SOURCING)


async def role_cards(db, role):
    """Set of cards the role may see, or None when never configured. Cards
    added after the role was saved fall back to their default."""
    if not role:
        return None
    row = (await db.execute(select(AppSetting).where(AppSetting.key == PREFIX + role))).scalar_one_or_none()
    if not row or row.value is None:
        return None
    try:
        data = json.loads(row.value)
    except (ValueError, TypeError):
        return None
    if isinstance(data, list):   # first format (staging only, 2026-11-15): every card was known
        data = {"cards": data, "known": AB_KEYS}
    if not isinstance(data, dict):
        return None
    cards, known = set(data.get("cards") or []), set(data.get("known") or [])
    out = {k for k in AB_KEYS if k in cards}
    new = [k for k in AB_KEYS if k not in known]
    if new:
        out |= set(new) & default_cards(role)
    return out


async def save_role_cards(db, role, keys, username):
    keys = [k for k in AB_KEYS if k in set(keys)]
    row = (await db.execute(select(AppSetting).where(AppSetting.key == PREFIX + role))).scalar_one_or_none()
    old = row.value if row else None
    val = json.dumps({"cards": keys, "known": AB_KEYS})
    if row:
        row.value, row.updated_by = val, username
    else:
        db.add(AppSetting(key=PREFIX + role, value=val, updated_by=username,
                          description="Activity Board cards visible to role %s" % role))
    return old, val
