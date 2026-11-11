"""Quotation numbers (2026-11-10, Pankaj): one shape for both kinds.

    Deal Quotes (CRM, crm_quotes)               QT-DEAL-<MMYYYY><NNNN>
    Telecalling Quotations (dealer_quotations)  QT-TELE-<MMYYYY><NNNN>

NNNN restarts at 0001 every month (the month is already in the number).
Both kinds share one status flow: Added -> Negotiation -> Approved /
Rejected / Expired.
"""
from datetime import date

from sqlalchemy import select, func

QUOTE_STATUSES = [("added", "Added"), ("negotiation", "Negotiation"), ("approved", "Approved"),
                  ("rejected", "Rejected"), ("expired", "Expired")]
QUOTE_STATUS_KEYS = [k for k, _ in QUOTE_STATUSES]
QUOTE_STATUS_COLORS = {"added": "secondary", "negotiation": "warning", "approved": "success",
                       "rejected": "danger", "expired": "dark"}
# Old values -> new (one-time mapping of existing rows, main.py)
OLD_STATUS_MAP = {"draft": "added", "sent": "added", "shared": "added",
                  "negotiating": "negotiation", "accepted": "approved", "confirmed": "approved"}


def month_prefix(kind: str, on: date | None = None) -> str:
    d = on or date.today()
    return f"QT-{kind}-{d.month:02d}{d.year}"


async def next_monthly_number(db, column, base: str, on: date | None = None) -> str:
    """Next free <base>-<MMYYYY><NNNN> in the month of `on`: highest existing
    NNNN in that month + 1 (gaps from deleted rows are never reused, and
    counting rows can't collide with a renumbered history)."""
    d = on or date.today()
    prefix = f"{base}-{d.month:02d}{d.year}"
    last = (await db.execute(
        select(func.max(column)).where(column.like(prefix + "%")))).scalar()
    n = int(last[len(prefix):]) + 1 if last and last[len(prefix):].isdigit() else 1
    return f"{prefix}{n:04d}"


async def next_quote_number(db, column, kind: str, on: date | None = None) -> str:
    """Next free quotation number for `kind` ('DEAL' / 'TELE')."""
    return await next_monthly_number(db, column, f"QT-{kind}", on)


# Deal IDs (2026-11-10, Pankaj): Supplier Deals SUPP-DEAL-<MMYYYY><NNNN>,
# Customer Deals CUST-DEAL-<MMYYYY><NNNN>; NNNN restarts every month.
SUPPLIER_DEAL_BASE = "SUPP-DEAL"
CUSTOMER_DEAL_BASE = "CUST-DEAL"
# Order IDs (2026-11-10, Pankaj): Sales Orders SO-DEAL-<MMYYYY><NNNN>,
# Purchase Orders PO-DEAL-<MMYYYY><NNNN>.
SALES_ORDER_BASE = "SO-DEAL"
PURCHASE_ORDER_BASE = "PO-DEAL"
