"""
Partner bid payments — the money leg of a won bid.

Status shown on Bids Created / My Bids / Partner Payments is DERIVED, never
stored, so the three pages can never disagree:

    no PO document attached        -> PO Pending
    SO / PO attached, no payment   -> Payment Due
    receipt shared, not verified   -> Payment Pending   (waiting on Accounts)
    payment row, verified          -> Payment Done      (approved on the Accounts board)

2026-11-16 (Pankaj): once the partner shares the payment receipt it reads
"Payment Pending" (the deal moves to its Payment stage); Accounts approving
it makes it "Payment Done".
"""
import uuid
from sqlalchemy import (Column, String, Numeric, DateTime, Text, Boolean,
                        ForeignKey)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base
from utils.timezone import app_now

PAYMENT_MODES = ["bank_transfer", "upi"]

# Derived status labels, in flow order.
PS_PO_PENDING = "PO Pending"
PS_PAYMENT_DUE = "Payment Due"
PS_PAYMENT_PENDING = "Payment Pending"
PS_PAYMENT_VERIFYING = PS_PAYMENT_PENDING   # old name, same state
PS_PAYMENT_DONE = "Payment Done"


class PartnerBidPayment(Base):
    """Payment details submitted by the partner against a won bid."""
    __tablename__ = "partner_bid_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bid_id = Column(UUID(as_uuid=True), ForeignKey("partner_bids.id"),
                    nullable=False, index=True)

    payment_date = Column(DateTime, nullable=True)
    payment_mode = Column(String(20), nullable=True)      # bank_transfer | upi
    payment_utr = Column(String(100), nullable=True)
    payment_amount = Column(Numeric(14, 2), nullable=True)
    notes = Column(Text, nullable=True)

    submitted_by = Column(String(100), nullable=True)     # partner/dealer identity
    submitted_at = Column(DateTime, default=app_now)

    verified = Column(Boolean, nullable=False, default=False)
    verified_by = Column(String(50), nullable=True)       # finance user
    verified_at = Column(DateTime, nullable=True)
    verify_notes = Column(Text, nullable=True)

    bid = relationship("PartnerBid", lazy="select")


def payment_status(has_po: bool, payment) -> str:
    """Single source of truth for the four-state payment label."""
    if not has_po:
        return PS_PO_PENDING
    if payment is None:
        return PS_PAYMENT_DUE
    return PS_PAYMENT_DONE if payment.verified else PS_PAYMENT_PENDING


PAYMENT_STATUS_BADGE = {
    PS_PO_PENDING:        "bg-secondary",
    PS_PAYMENT_DUE:       "bg-warning text-dark",
    PS_PAYMENT_PENDING:   "bg-info text-dark",
    PS_PAYMENT_DONE:      "bg-success",
}
