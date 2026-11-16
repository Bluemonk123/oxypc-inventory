"""OxyPC Trade Partner — dealer-facing B2B portal models.

All tables prefixed partner_ (the existing dealer_* tables belong to the
internal telecalling module). Dealer identity itself stays on the existing
`dealers` table (portal columns added there) so the dealer master remains
the single source of truth.
"""
import uuid
from utils.timezone import app_now
from sqlalchemy import (
    Column, String, DateTime, ForeignKey, Text, Boolean, Numeric, Integer,
    Index, text,
)
from sqlalchemy.dialects.postgresql import UUID
from decimal import Decimal, ROUND_CEILING
from sqlalchemy.orm import relationship
from database import Base


# ── Listing types / statuses (plain varchars, validated in service layer) ──
LISTING_TYPES = ["finished_goods", "bulk_lot", "functional_ok", "as_is"]
LISTING_STATUSES = ["draft", "published", "paused", "sold_out"]
BOOKING_STATUSES = [
    "pending_payment", "proof_uploaded", "confirmed_token",
    "balance_pending", "ready_for_dispatch", "dispatched",
    "rejected", "expired", "cancelled",
]
PRICE_SEGMENTS = ["new_dealer", "verified", "high_volume", "city_captain", "trader"]


class PartnerListing(Base):
    __tablename__ = "partner_listings"
    __table_args__ = (
        Index("ix_partner_listings_status_type", "status", "listing_type"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_code = Column(String(20), unique=True, nullable=False, index=True)  # TPL-0001
    listing_type = Column(String(20), nullable=False)  # finished_goods, bulk_lot, functional_ok, as_is
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    brand = Column(String(100), nullable=True)
    model_summary = Column(String(300), nullable=True)
    grade_summary = Column(String(100), nullable=True)   # e.g. "A/B mix"
    qty_total = Column(Integer, nullable=False, default=0)
    qty_available = Column(Integer, nullable=False, default=0)
    moq = Column(Integer, nullable=False, default=1)
    dealer_price = Column(Numeric(12, 2), nullable=False, default=0)   # per unit
    token_amount = Column(Numeric(12, 2), nullable=False, default=0)   # resolved ₹ per unit
    hold_hours = Column(Integer, nullable=False, default=24)
    photos = Column(Text, nullable=True)  # JSON list of filenames under uploads/partner/
    status = Column(String(20), nullable=False, default="draft")
    # Margin guardrail
    cost_basis = Column(Numeric(12, 2), nullable=True)      # internal reference, NEVER dealer-exposed
    floor_value = Column(Numeric(12, 2), nullable=True)      # resolved floor at publish time
    floor_override_by = Column(String(50), nullable=True)    # username; set only on audited override
    floor_override_reason = Column(Text, nullable=True)
    # Price tiering readiness (Phase 1 uses standard price for all)
    price_tier = Column(String(30), nullable=False, default="standard")
    visible_to_segment = Column(String(30), nullable=False, default="all")
    price_reviewed_at = Column(DateTime, nullable=True)      # 48h stale-price rule
    # Ageing source: intake date of underlying stock (lot created_at / GRN date)
    stock_intake_date = Column(DateTime, nullable=True)
    created_by = Column(String(50), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    devices = relationship("PartnerListingDevice", back_populates="listing", lazy="select")


class PartnerListingDevice(Base):
    """Ready-stock devices backing a finished-goods listing (blocks double-listing)."""
    __tablename__ = "partner_listing_devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_id = Column(UUID(as_uuid=True), ForeignKey("partner_listings.id"), nullable=False, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=app_now)

    listing = relationship("PartnerListing", back_populates="devices")


class PartnerFloorConfig(Base):
    """Versioned margin floors per listing type — never overwritten, effective-dated."""
    __tablename__ = "partner_floor_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_type = Column(String(20), nullable=False, index=True)
    floor_rule_type = Column(String(30), nullable=False)  # margin_pct, floor_price_pct, recovery_value, liquidation_min
    floor_pct = Column(Numeric(6, 2), nullable=True)      # e.g. 8.00 = 8% over cost basis
    floor_value = Column(Numeric(12, 2), nullable=True)   # absolute ₹ floor where applicable
    effective_from = Column(DateTime, nullable=False, default=app_now)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)


class PartnerBooking(Base):
    __tablename__ = "partner_bookings"
    __table_args__ = (
        Index("ix_partner_bookings_status_expiry", "status", "expires_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_number = Column(String(30), unique=True, nullable=False, index=True)  # TPB-0001
    listing_id = Column(UUID(as_uuid=True), ForeignKey("partner_listings.id"), nullable=False, index=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False, index=True)
    qty = Column(Integer, nullable=False)
    unit_price_snapshot = Column(Numeric(12, 2), nullable=False)
    token_per_unit_snapshot = Column(Numeric(12, 2), nullable=False)
    token_total = Column(Numeric(14, 2), nullable=False)
    status = Column(String(30), nullable=False, default="pending_payment", index=True)
    expires_at = Column(DateTime, nullable=False)
    rejection_reason = Column(Text, nullable=True)
    confirmed_by = Column(String(50), nullable=True)
    balance_verified_by = Column(String(50), nullable=True)
    dispatched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    listing = relationship("PartnerListing", lazy="select")
    dealer = relationship("Dealer", lazy="select")
    proofs = relationship("PartnerPaymentProof", back_populates="booking", lazy="select")

    @property
    def balance_due(self):
        return (self.unit_price_snapshot * self.qty) - self.token_total


class PartnerPaymentProof(Base):
    __tablename__ = "partner_payment_proofs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("partner_bookings.id"), nullable=False, index=True)
    proof_type = Column(String(10), nullable=False, default="token")  # token | balance
    utr_reference = Column(String(100), nullable=True)
    amount_claimed = Column(Numeric(14, 2), nullable=True)
    screenshot_path = Column(String(500), nullable=True)  # stored filename under uploads/partner/
    status = Column(String(20), nullable=False, default="pending")  # pending, verified, rejected
    uploaded_at = Column(DateTime, default=app_now)
    verified_by = Column(String(50), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    booking = relationship("PartnerBooking", back_populates="proofs")


class PartnerSetting(Base):
    """Key/value settings for the partner portal (hold hours, token %, bank details,
    WhatsApp launch template, incentive text)."""
    __tablename__ = "partner_settings"

    key = Column(String(50), primary_key=True)
    value = Column(Text, nullable=True)
    updated_by = Column(String(50), nullable=True)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)


class PartnerLoginLog(Base):
    __tablename__ = "partner_login_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=True, index=True)
    phone_attempted = Column(String(30), nullable=True)
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(String(300), nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=app_now, index=True)


class PartnerListingView(Base):
    """Raw dealer-engagement feed for the dealer score."""
    __tablename__ = "partner_listing_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_id = Column(UUID(as_uuid=True), ForeignKey("partner_listings.id"), nullable=False, index=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False, index=True)
    viewed_at = Column(DateTime, default=app_now)


class LotDealerVisibility(Base):
    """Trade Partner — Manage Lots (item 27): which raw Lots (not curated
    PartnerListings) a given dealer is allowed to see in their Catalog.
    Deliberately separate from PartnerListing/PartnerBooking's pricing and
    token-payment engine — this is direct Lot Management visibility, not a
    priced listing."""
    __tablename__ = "lot_dealer_visibility"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=False, index=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False, index=True)
    assigned_by = Column(String(50), nullable=True)
    assigned_at = Column(DateTime, default=app_now)

    __table_args__ = (
        Index("ix_lot_dealer_visibility_unique", "lot_id", "dealer_id", unique=True),
    )


class LotBookingRequest(Base):
    """Trade Partner Catalog — item 28's Book/Withdraw action on a visible
    Lot. Kept as its own lightweight request record (qty + status only, no
    pricing/token math) rather than forced into PartnerBooking's financial
    engine, which is priced against PartnerListing fields Lots don't have."""
    __tablename__ = "lot_booking_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=False, index=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False, index=True)
    qty_requested = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="requested", index=True)  # requested | approved | withdrawn
    # Approve in Bookings Queue (2026-11-15, Pankaj — approved schema change):
    # the Customer Deal it opened (plain UUID like PartnerBid.opportunity_id —
    # CRM is another bounded context) and who approved it.
    opportunity_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    approved_by = Column(String(50), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)


# ── Bidding ───────────────────────────────────────────────────────────────
#
# An auction layer over the existing catalog. Kept as ONE table with a
# bid_type discriminator rather than separate "captured" and "custom" tables:
# both are a dealer naming a price against a listing, they sort against each
# other on the same Bids page, and either can be the one that is Marked Won.
# Splitting them would mean two Mark-Won paths and two ways to be the highest.

BID_TYPES = ["increment", "custom"]
BID_STATUSES = ["active", "won", "lost", "withdrawn"]

# Each bid must clear the previous one (or the listing's base price when there
# is none) by this much. Held here rather than inline so the rule has one home.
BID_INCREMENT_PCT = Decimal("0.10")


def min_next_bid(base_price, highest_bid=None) -> Decimal:
    """Lowest amount the next bid may be.

    "10% on the base price or the last bid" — so the reference is the standing
    highest bid once there is one, and the listing's base price before that.

    Rounded UP to the whole rupee: the raw product routinely lands on a
    fraction, and showing a dealer "minimum 12100.000000001" then rejecting
    12100 is indefensible. Rounding up rather than to-nearest keeps the
    displayed minimum always actually acceptable to the server.
    """
    ref = Decimal(str(highest_bid if highest_bid is not None else base_price or 0))
    return (ref * (Decimal("1") + BID_INCREMENT_PCT)).quantize(
        Decimal("1"), rounding=ROUND_CEILING)


class PartnerBid(Base):
    """A dealer's bid against a listing — auction increment or custom offer."""
    __tablename__ = "partner_bids"
    __table_args__ = (
        # The Bids page sorts by amount within a listing, and the partner side
        # reads "highest active bid for this listing" on every catalog render.
        Index("ix_partner_bids_listing_amount", "listing_id", "bid_amount"),
        Index("ix_partner_bids_status_created", "status", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bid_number = Column(String(30), unique=True, nullable=False, index=True)  # BID-0001
    # A bid targets EITHER a published listing or a Lot the dealer can see on
    # the catalog. Both are "a thing a dealer names a price against", they share
    # the increment rule, the Bids page and Mark Won — so they share one table
    # rather than forking the whole subsystem. Exactly one of these is set;
    # bid_target below is the single place that decides which.
    listing_id = Column(UUID(as_uuid=True), ForeignKey("partner_listings.id"),
                        nullable=True, index=True)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"),
                    nullable=True, index=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"),
                       nullable=False, index=True)
    bid_amount = Column(Numeric(14, 2), nullable=False)
    bid_type = Column(String(20), nullable=False, default="increment")
    # What the bid had to beat, and the listing price at the time. Snapshotted
    # because the listing can be repriced afterwards, and without these a bid
    # cannot be audited against the rule that was actually in force.
    previous_amount = Column(Numeric(14, 2), nullable=True)
    base_amount = Column(Numeric(14, 2), nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    notes = Column(Text, nullable=True)          # dealer's note on a custom price
    won_by = Column(String(50), nullable=True)   # staff username who marked it won
    won_at = Column(DateTime, nullable=True)
    # The Buyer Deal (CRMSalesOpportunity) created by Mark Won. Plain UUID, not
    # an FK: crm_sales_opportunities lives in the CRM bounded context and a
    # hard cross-context FK is exactly what the schema rules warn against.
    opportunity_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    listing = relationship("PartnerListing", lazy="select")
    lot = relationship("Lot", lazy="select")
    dealer = relationship("Dealer", lazy="select")
    documents = relationship("PartnerBidDocument", back_populates="bid", lazy="select")

    @property
    def bid_target(self) -> str:
        """'listing' or 'lot' — which of the two this bid is against."""
        return "lot" if self.lot_id else "listing"


class PartnerBidDocument(Base):
    """Quote / PO / Invoice attached to a bid, for the Bids page download column."""
    __tablename__ = "partner_bid_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bid_id = Column(UUID(as_uuid=True), ForeignKey("partner_bids.id"),
                    nullable=False, index=True)
    doc_type = Column(String(20), nullable=False)   # quote | po | invoice
    filename = Column(String(300), nullable=False)  # stored under uploads/partner/bids/
    original_name = Column(String(300), nullable=True)
    uploaded_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)

    bid = relationship("PartnerBid", back_populates="documents")


BID_DOC_TYPES = ["quote", "so", "po", "invoice"]   # "so" = Sales Order from Mark Won (2026-11-15)


class PartnerNotification(Base):
    """Bell icon + live toast on the Trade Partner portal (2026-11-15, Pankaj —
    approved schema change). Staff notifications (models/notification.py) hang
    off users.id, so partners get their own table keyed by dealers.id: booking
    approved, bid won / lost, Sales Order or PO attached, payment verified."""
    __tablename__ = "partner_notifications"
    __table_args__ = (Index("ix_partner_notifications_dealer_read", "dealer_id", "is_read"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=True)
    kind = Column(String(20), nullable=False, default="info")   # info / success / warning
    link = Column(String(300), nullable=True)                   # portal page to open
    is_read = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, default=app_now, index=True)
