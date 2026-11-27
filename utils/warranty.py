"""
OxyPC Warranty Utility
======================
Single source of truth for device warranty derivation.

Policy (2026-11-27, Pankaj — one rule everywhere): a tag is in warranty only
when a warranty was picked on its sale (Sale.warranty_type, with
Sale.warranty_expires_at, pushed forward by Extended Warranty) and today is on
or before that end date. No warranty picked = No Warranty. There is no longer
a 30-days-from-sale default; use effective_warranty_for_sale /
warranty_status_for_sale (and latest_sale_map for lists).

warranty_from_sold_at below is the old 30-day rule, kept only for callers
outside this app's pages; nothing on a page should use it.

Definition (per spec):
  - Active   -> "Warranty Left: <N> days"  where N = 30 - days_passed_since_sale
  - Expired  -> "Warranty Expired on <DD Mon YYYY>"  (sale_date + 30 days)

Used by Ready to Sale, Ready to Dispatch, Process Return and L3 replacement views.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from utils.timezone import app_now

WARRANTY_DAYS = 30


def warranty_from_sold_at(sold_at: datetime | None) -> dict | None:
    """Return a warranty descriptor dict for a given sale timestamp, or None if
    there is no sale (hence no warranty).

    Dict shape:
        {"status": "active"|"expired", "days_left": int, "expiry": datetime,
         "label": str}
    """
    if not sold_at:
        return None
    expiry = sold_at + timedelta(days=WARRANTY_DAYS)
    now = app_now()
    if now <= expiry:
        days_left = (expiry.date() - now.date()).days
        if days_left < 0:
            days_left = 0
        return {
            "status": "active",
            "days_left": days_left,
            "expiry": expiry,
            "label": f"Warranty Left: {days_left} days",
        }
    return {
        "status": "expired",
        "days_left": 0,
        "expiry": expiry,
        "label": f"Warranty Expired on {expiry.strftime('%d %b %Y')}",
    }


def warranty_label(sold_at: datetime | None, none_text: str = "—") -> str:
    """Convenience: just the display label, with a fallback when no warranty."""
    w = warranty_from_sold_at(sold_at)
    return w["label"] if w else none_text


WARRANTY_DURATIONS = {
    "none": None,
    "30_days": timedelta(days=30),
    "6_months": timedelta(days=182),
    "1_year": timedelta(days=365),
}


def compute_warranty_expiry(sold_at: datetime, warranty_type: str) -> datetime | None:
    """Server-side computation of warranty_expires_at from sold_at + warranty_type.
    Returns None for warranty_type == 'none' or unrecognized values.

    Only understands the 3 fixed legacy slugs in WARRANTY_DURATIONS — use
    parse_warranty_duration() + sold_at + timedelta(days=days) instead
    wherever warranty_type comes from the admin-editable sale_warranty_type
    Master Data list (New Sale, Extended Warranty), which can hold any
    "<N> day/month/year" label, not just these three."""
    if not sold_at:
        return None
    delta = WARRANTY_DURATIONS.get(warranty_type)
    if delta is None:
        return None
    return sold_at + delta


_WARRANTY_DURATION_RE = re.compile(r'(\d+)[\s_-]*(day|month|year)')


def parse_warranty_duration(raw: str) -> tuple[str | None, int | None]:
    """Parse a Master Data Warranty Type label into (canonical_slug, days).

    A fixed 3-entry lookup table (30_days/6_months/1_year, see
    WARRANTY_DURATIONS) breaks the moment an admin adds "60 Days"/"90 Days"
    to the Master Data Dropdown Configuration, since neither is in the table
    ("Select a valid Warranty Type" on a perfectly valid pick). This parses
    the "<N> day/month/year" pattern generically instead —
    case/spacing/underscore-insensitive — so ANY admin-added value (any N,
    any unit) works with no further code change. "No Warranty" / "none" /
    the blank placeholder all lack a digit+unit pattern and correctly fall
    through to (None, None) — not a real duration, same as an unrecognized
    value.

    Single source for this parsing — New Sale (routers/sales.py) and
    Extended Warranty (routers/extended_warranty.py) both read the same
    admin-editable sale_warranty_type Master Data list and must agree on
    what a given label means; New Sale silently defaulted every sale to "No
    Warranty" for a long time because it checked warranty_type against the
    fixed WARRANTY_DURATIONS keys instead of parsing it this way.
    """
    m = _WARRANTY_DURATION_RE.search((raw or '').lower())
    if not m:
        return None, None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == 'day':
        days = n
    elif unit == 'month':
        # Preserve the pre-existing "6 Months" = 182 days convention exactly;
        # any other month count uses the average month length.
        days = 182 if n == 6 else round(n * 30.44)
    else:
        days = n * 365
    slug = f"{n}_{unit}" + ('' if n == 1 else 's')
    return slug, days


def effective_warranty_for_sale(sale) -> dict | None:
    """Same descriptor shape as warranty_from_sold_at (status/days_left/expiry/
    label), but gives precedence to an explicit Sale.warranty_type +
    warranty_expires_at (set at sale time, or pushed forward by Extended
    Warranty) over the implicit WARRANTY_DAYS-from-sold_at default. Falls back
    to warranty_from_sold_at when this sale never had a warranty_type chosen.

    Use this instead of warranty_from_sold_at anywhere the result needs to
    agree with warranty_status_for_sale (the RMA/Extended Warranty status) for
    the same sale — warranty_from_sold_at never looks at warranty_expires_at
    at all, so after a warranty extension it kept reporting "Out of Warranty"
    on Process Return's "Warranty Status" field while RMA Warranty correctly
    said "In Warranty" for the same tag, and Process Return's paid-repair-
    charge banner keyed off the stale one.

    2026-11-27 (Pankaj): no 30-day default any more — a sale with no warranty
    picked has no warranty (None), the same as warranty_status_for_sale.
    """
    if not sale:
        return None
    wtype = getattr(sale, "warranty_type", None) or "none"
    expires_at = getattr(sale, "warranty_expires_at", None)
    if wtype == "none" or not expires_at:
        return None
    now = app_now()
    if now <= expires_at:
        days_left = max((expires_at.date() - now.date()).days, 0)
        return {
            "status": "active", "days_left": days_left, "expiry": expires_at,
            "label": f"Warranty Left: {days_left} days",
        }
    return {
        "status": "expired", "days_left": 0, "expiry": expires_at,
        "label": f"Warranty Expired on {expires_at.strftime('%d %b %Y')}",
    }


def warranty_status_for_sale(sale) -> str:
    """Return 'in_warranty' | 'out_of_warranty' | 'no_warranty' for a Sale record,
    using the Sale's own warranty_type/warranty_expires_at (Phase 1a fields).
    Falls back to 'no_warranty' when there is no sale or no warranty was selected."""
    if not sale:
        return "no_warranty"
    wtype = getattr(sale, "warranty_type", None) or "none"
    if wtype == "none":
        return "no_warranty"
    expires_at = getattr(sale, "warranty_expires_at", None)
    if not expires_at:
        return "no_warranty"
    now = app_now()
    return "in_warranty" if now <= expires_at else "out_of_warranty"


async def latest_sold_at_map(db, device_ids) -> dict:
    """Return {device_id(str): latest Sale.sold_at} for the given device ids.

    Used to build warranty columns without N+1 queries. Imports are local to
    avoid a circular import with models at module load.
    """
    from sqlalchemy import select, func
    from models.sales import Sale

    out: dict = {}
    if not device_ids:
        return out
    rows = (await db.execute(
        select(Sale.device_id, func.max(Sale.sold_at))
        .where(Sale.device_id.in_(list(device_ids)))
        .group_by(Sale.device_id)
    )).all()
    for did, sold_at in rows:
        out[str(did)] = sold_at
    return out


async def latest_sale_map(db, device_ids) -> dict:
    """{device_id(str): latest Sale} for the given device ids — for list pages
    that show each tag's warranty with effective_warranty_for_sale (2026-11-27)."""
    from sqlalchemy import select
    from models.sales import Sale

    out: dict = {}
    ids = list(device_ids or [])
    for i in range(0, len(ids), 5000):
        rows = (await db.execute(
            select(Sale).where(Sale.device_id.in_(ids[i:i + 5000]))
            .order_by(Sale.device_id, Sale.sold_at.desc().nullslast())
            .distinct(Sale.device_id)
        )).scalars().all()
        for s in rows:
            out[str(s.device_id)] = s
    return out
