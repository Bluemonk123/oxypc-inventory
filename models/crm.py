"""
CRM Module Models — OxyPC Inventory
Tables: crm_contacts, crm_sourcing_deals, crm_sales_opportunities,
        crm_quotes, crm_quote_items, crm_activities
"""
import uuid
from datetime import datetime, date
from utils.timezone import app_now
from sqlalchemy import (
    Column, String, DateTime, Numeric, Integer, Text, Boolean, ForeignKey, Date,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


# ── SOURCE TYPES (suppliers we buy from) ─────────────────────────────────────
SOURCE_TYPES = [
    ("recycler",  "Recycler"),
    ("refurb",    "Refurbisher"),
    ("endcust",   "End Customer (Corporate)"),
    ("trader",    "Trader / Broker"),
    ("indiv",     "Individual"),
    ("online",    "Online Source"),
]

# ── BUYER TYPES (who we sell to) ─────────────────────────────────────────────
BUYER_TYPES = [
    ("corp_buyer",    "Corporate Buyer"),
    ("dealer",        "Dealer"),
    ("online_seller", "Online Seller"),
    ("export",        "Export Trader"),
    ("retail",        "Retail Customer"),
    ("gov",           "Government / NGO"),
]

# ── SOURCING DEAL STAGES ──────────────────────────────────────────────────────
# Reworked 2026-10-27 (old keys quoted / agreed / po_raised are mapped once at
# startup by main._migrate_sourcing_stages_once: quoted -> quote,
# agreed -> negotiation, po_raised -> purchase_order).
SOURCING_STAGES = [
    ("lead",           "Lead Identified"),
    ("contacted",      "Contacted"),
    ("inspection",     "Inspection"),
    ("quote",          "Quote"),
    ("negotiation",    "Negotiations"),
    ("purchase_order", "Purchase Order"),
    ("payment",        "Payment"),
    ("received",       "Stock Received"),
    ("won",            "Closed — Won"),
    ("lost",           "Closed — Lost"),
]

# Items shown under each stage in Stage Progress, stored per deal in
# crm_sourcing_steps (one row per deal + step_key).
#   kind "user"     — assign a user (user icon opens the Assign modal)
#   kind "approval" — assign an approver, who then Approves / Rejects
#   kind "file"     — attach a document (shown on the right)
# Purchase Order's document is the latest PO generated from the deal — shown
# automatically, nothing to attach.
SOURCING_STAGE_STEPS = {
    "inspection": [
        ("inspection_assign", "Assign to Inspection", "user"),
        ("inspection_report", "Inspection Report", "file"),
    ],
    "quote": [
        ("quote_l1", "Level 1", "approval"),
        ("quote_l2", "Level 2", "approval"),
        ("quote_file", "Quote", "file"),
    ],
    "negotiation": [
        ("negotiation_l1", "Level 1", "approval"),
        ("negotiation_quote", "Updated Quote", "file"),
    ],
    "purchase_order": [
        ("po_l1", "Level 1", "approval"),
        ("po_l2", "Level 2", "approval"),
    ],
    "payment": [
        ("payment_l1", "Level 1", "approval"),
        ("payment_invoice", "Invoice", "file"),
    ],
    "received": [
        ("received_assign", "Assign to Inventory", "user"),
    ],
}
SOURCING_STEP_INDEX = {key: (stage, label, kind)
                       for stage, steps in SOURCING_STAGE_STEPS.items()
                       for key, label, kind in steps}

# ── SALES OPPORTUNITY STAGES ──────────────────────────────────────────────────
SALES_STAGES = [
    ("lead",         "Lead / Enquiry"),
    ("contacted",    "Contacted"),
    ("requirement",  "Requirement Understood"),
    ("availability", "Availability Confirmed"),
    ("quoted",       "Quote Sent"),
    ("negotiation",  "Negotiation"),
    ("confirmed",    "Order Confirmed"),
    ("invoiced",     "Invoice Raised"),
    ("delivered",    "Delivered"),
    ("payment",      "Payment Received"),
    ("won",          "Closed — Won"),
    ("lost",         "Closed — Lost"),
]

# ── MATERIAL TYPES ────────────────────────────────────────────────────────────
MATERIAL_TYPES = [
    ("as_is_untested",  "As-Is Untested"),
    ("as_is_tested",    "As-Is Tested / Graded"),
    ("as_is_graded",    "As-Is Graded"),
    ("partially_refurb","Partially Refurbished"),
    ("refurb_full",     "Fully Refurbished"),
    ("scrap_parts",     "Scrap / Parts Only"),
    ("bulk_mix",        "Bulk Mixed Lot"),
]

GRADES = ["A", "B", "C", "D", "Mix", "As-Is", "Scrap"]
PRIORITIES = ["low", "medium", "high", "urgent"]
ACTIVITY_TYPES = ["call", "whatsapp", "visit", "email", "meeting", "note"]
ACTIVITY_OUTCOMES = ["interested", "not_interested", "callback", "order_placed",
                     "no_answer", "followup", "done", "rescheduled"]


class CRMContact(Base):
    """Unified buyer + supplier contact registry."""
    __tablename__ = "crm_contacts"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_code     = Column(String(20), unique=True, nullable=False, index=True)  # CRM0001
    contact_type     = Column(String(20), default="supplier")  # supplier / buyer / both
    company_name     = Column(String(200), nullable=False, index=True)
    contact_person   = Column(String(100), nullable=True)
    phone            = Column(String(20),  nullable=True, index=True)
    whatsapp         = Column(String(20),  nullable=True)
    email            = Column(String(100), nullable=True)
    gstin            = Column(String(20),  nullable=True)
    pan              = Column(String(20),  nullable=True)
    address          = Column(Text,        nullable=True)
    city             = Column(String(100), nullable=True)
    state            = Column(String(100), nullable=True)
    pincode          = Column(String(10),  nullable=True)
    # supplier classification
    source_type      = Column(String(30),  nullable=True)   # recycler/refurb/endcust/trader/indiv/online
    # buyer classification
    buyer_type       = Column(String(30),  nullable=True)   # corp_buyer/dealer/online_seller/export/retail/gov
    credit_limit     = Column(Numeric(14, 2), default=0)
    outstanding      = Column(Numeric(14, 2), default=0)
    tags             = Column(String(300), nullable=True)   # comma-separated
    notes            = Column(Text,        nullable=True)
    status           = Column(String(20),  default="active")   # active/inactive/blacklisted
    assigned_to      = Column(String(50),  nullable=True)
    created_by       = Column(String(50),  nullable=True)
    created_at       = Column(DateTime,    default=app_now)
    updated_at       = Column(DateTime,    default=app_now, onupdate=app_now)
    is_trashed       = Column(Boolean,     default=False, nullable=False, server_default="false")

    # DPDPA 2023 readiness (Nov-2025 rules; enforcement May-2027).
    # Fields land now; trigger-based enforcement deferred to Phase 1.5.
    consent_recorded      = Column(Boolean,  nullable=False, default=False)
    consent_at            = Column(DateTime, nullable=True)
    consent_source        = Column(String(40), nullable=True)  # telecalling_mobile|webform|whatsapp_optin|dealer_signup
    do_not_contact        = Column(Boolean,  nullable=False, default=False, index=True)
    do_not_contact_reason = Column(String(200), nullable=True)

    # ── KYC Documents (Add Account page) ────────────────────────────────────
    gstin_doc_path          = Column(String(255), nullable=True)
    pan_doc_path            = Column(String(255), nullable=True)
    bank_cheque_number      = Column(String(50),  nullable=True)
    bank_cheque_doc_path    = Column(String(255), nullable=True)
    msme_number             = Column(String(50),  nullable=True)
    msme_doc_path           = Column(String(255), nullable=True)
    director1_id_number     = Column(String(50),  nullable=True)
    director1_doc_path      = Column(String(255), nullable=True)
    director2_id_number     = Column(String(50),  nullable=True)
    director2_doc_path      = Column(String(255), nullable=True)

    # ── KYC verification (Account Detail "Verify KYC" action) ───────────────
    kyc_verified    = Column(Boolean,  nullable=False, default=False, server_default="false")
    kyc_verified_by = Column(String(50), nullable=True)
    kyc_verified_at = Column(DateTime, nullable=True)

    sourcing_deals   = relationship("CRMSourcingDeal",    back_populates="contact", lazy="select")
    sales_opps       = relationship("CRMSalesOpportunity", back_populates="contact", lazy="select")
    activities       = relationship("CRMActivity",         back_populates="contact", lazy="select")
    quotes           = relationship("CRMQuote",            back_populates="contact", lazy="select")
    contact_numbers  = relationship("CRMContactNumber",    back_populates="contact",
                                    cascade="all, delete-orphan",
                                    order_by="CRMContactNumber.sort_order")
    locations = relationship(
        "CRMContactLocation", back_populates="contact",
        cascade="all, delete-orphan", order_by="CRMContactLocation.sort_order",
    )


class CRMContactLocation(Base):
    """A site/branch location for a CRM contact (one contact → many locations).

    Sits alongside the contact's own Company Details address (which is left
    untouched) — this is the "Locations" section on the New/Edit Account form.
    Each location owns its own set of CRMContactNumber rows (moved here from
    being contact-level, per the 2026-09-23 Locations feature).
    """
    __tablename__ = "crm_contact_locations"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id    = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=False, index=True)
    contact_email = Column(String(100), nullable=True)
    address       = Column(String(300), nullable=True)
    city          = Column(String(100), nullable=True)
    state         = Column(String(100), nullable=True)
    sort_order    = Column(Integer, default=0)
    created_at    = Column(DateTime, default=app_now)

    contact = relationship("CRMContact", back_populates="locations")
    contact_numbers = relationship(
        "CRMContactNumber", back_populates="location",
        cascade="all, delete-orphan", order_by="CRMContactNumber.sort_order",
    )


class CRMContactNumber(Base):
    """Additional person + phone entries for a contact location (one location → many numbers).

    The parent CRMContact keeps its single primary `phone`/`whatsapp`; this child
    table holds the extra people/numbers added via the "Contact Numbers" section
    nested under each Location on the New/Edit Contact form. Managed as line-items
    (cascade delete-orphan), mirroring CRMQuoteItem / CRMPOLineItem.

    `contact_id` is legacy (pre-2026-09-24 rows were linked directly to the
    contact, before the Locations feature existed) — left in place, unused by
    new writes, rather than dropped. `location_id` is the live FK going forward.
    """
    __tablename__ = "crm_contact_numbers"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id  = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True, index=True)
    location_id = Column(UUID(as_uuid=True), ForeignKey("crm_contact_locations.id"), nullable=True, index=True)
    person_role = Column(String(30),  nullable=True)   # Directors/Finance/Manager/Other
    person_name = Column(String(100), nullable=True)
    phone       = Column(String(20),  nullable=True)
    email       = Column(String(100), nullable=True)
    sort_order  = Column(Integer,     default=0)
    created_at  = Column(DateTime,    default=app_now)

    contact = relationship("CRMContact", back_populates="contact_numbers")
    location = relationship("CRMContactLocation", back_populates="contact_numbers")


class CRMSourcingDeal(Base):
    """Purchase/sourcing pipeline — from lead to lot creation."""
    __tablename__ = "crm_sourcing_deals"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deal_number          = Column(String(30), unique=True, nullable=False, index=True)  # SD-2025-0001
    contact_id           = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True, index=True)
    title                = Column(String(300), nullable=False)
    source_type          = Column(String(30),  nullable=True)
    device_type          = Column(String(50),  nullable=True)   # laptop/desktop/mix/tablet
    est_quantity         = Column(Integer,     nullable=True)
    material_type        = Column(String(30),  nullable=True)
    # financials
    asking_price_unit    = Column(Numeric(10, 2), nullable=True)
    asking_price_total   = Column(Numeric(14, 2), nullable=True)
    our_offer_unit       = Column(Numeric(10, 2), nullable=True)
    our_offer_total      = Column(Numeric(14, 2), nullable=True)
    final_price_unit     = Column(Numeric(10, 2), nullable=True)
    final_price_total    = Column(Numeric(14, 2), nullable=True)
    # pipeline
    stage                = Column(String(30),  default="lead", index=True)
    # inspection
    inspection_date      = Column(Date,        nullable=True)
    inspection_result    = Column(String(20),  nullable=True)   # pass/fail/conditional
    inspection_notes     = Column(Text,        nullable=True)
    # logistics
    expected_pickup_date = Column(Date,        nullable=True)
    payment_advance_pct  = Column(Integer,     default=0)
    payment_terms        = Column(Text,        nullable=True)
    # outcome
    linked_lot_id        = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=True)
    win_loss_reason      = Column(Text,        nullable=True)
    # metadata
    assigned_to          = Column(String(50),  nullable=True)
    priority             = Column(String(10),  default="medium")
    notes                = Column(Text,        nullable=True)
    product_records_file = Column(String(500), nullable=True)   # stored filename under uploads/crm/
    purchase_invoice_path= Column(String(500), nullable=True)   # stored filename under uploads/crm/
    purchase_order_path  = Column(String(500), nullable=True)   # stored filename under uploads/crm/
    eway_bill_path       = Column(String(500), nullable=True)   # stored filename under uploads/crm/
    quote_received_path  = Column(String(500), nullable=True)   # stored filename under uploads/crm/
    created_by           = Column(String(50),  nullable=True)
    created_at           = Column(DateTime,    default=app_now)
    updated_at           = Column(DateTime,    default=app_now, onupdate=app_now)

    contact    = relationship("CRMContact", back_populates="sourcing_deals")
    activities = relationship("CRMActivity", foreign_keys="CRMActivity.deal_id",
                              primaryjoin="and_(CRMActivity.deal_id == CRMSourcingDeal.id, "
                                          "CRMActivity.deal_type == 'sourcing')",
                              lazy="select", overlaps="activities")
    po_line_items = relationship("CRMSourcingDealLineItem", back_populates="deal",
                                 cascade="all, delete-orphan",
                                 order_by="CRMSourcingDealLineItem.sort_order")


class CRMSourcingStep(Base):
    """One Stage Progress item on a Sourcing Deal (2026-10-27): an assigned
    user, an approval or an attached document — see SOURCING_STAGE_STEPS.
    Approved on dbdiagram.io."""
    __tablename__ = "crm_sourcing_steps"
    __table_args__ = (UniqueConstraint("deal_id", "step_key", name="uq_crm_sourcing_steps_deal_step"),)

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deal_id     = Column(UUID(as_uuid=True), ForeignKey("crm_sourcing_deals.id"), nullable=False, index=True)
    stage       = Column(String(30),  nullable=False)
    step_key    = Column(String(40),  nullable=False)
    assigned_to = Column(String(50),  nullable=True)    # username
    status      = Column(String(20),  nullable=False, default="pending")  # pending / approved / rejected / done
    file_path   = Column(String(500), nullable=True)    # stored filename under uploads/crm/
    acted_by    = Column(String(50),  nullable=True)    # who approved / rejected / attached
    acted_at    = Column(DateTime,    nullable=True)
    note        = Column(Text,        nullable=True)    # approval remark
    created_by  = Column(String(50),  nullable=True)
    created_at  = Column(DateTime,    default=app_now)
    updated_at  = Column(DateTime,    default=app_now, onupdate=app_now)


class CRMSalesOpportunity(Base):
    """Sales opportunity pipeline — from enquiry to sale linkage."""
    __tablename__ = "crm_sales_opportunities"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opp_number         = Column(String(30), unique=True, nullable=False, index=True)  # OPP-2025-0001
    contact_id         = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True, index=True)
    title              = Column(String(300), nullable=False)
    buyer_type         = Column(String(30),  nullable=True)
    device_type        = Column(String(50),  nullable=True)
    required_qty       = Column(Integer,     nullable=True)
    material_type      = Column(String(30),  nullable=True)
    grade_required     = Column(String(10),  nullable=True)   # A/B/C/D/Mix
    budget_per_unit    = Column(Numeric(10, 2), nullable=True)
    # pipeline
    stage              = Column(String(30),  default="lead", index=True)
    quote_id           = Column(UUID(as_uuid=True), ForeignKey("crm_quotes.id"), nullable=True)
    linked_sale_ids    = Column(Text,        nullable=True)   # comma-sep sale UUIDs
    expected_close_date= Column(Date,        nullable=True)
    estimated_value    = Column(Numeric(14, 2), nullable=True)
    win_loss_reason    = Column(Text,        nullable=True)
    # metadata
    assigned_to        = Column(String(50),  nullable=True)
    priority           = Column(String(10),  default="medium")
    notes              = Column(Text,        nullable=True)
    product_records_file = Column(String(500), nullable=True)  # stored filename under uploads/crm/
    created_by         = Column(String(50),  nullable=True)
    created_at         = Column(DateTime,    default=app_now)
    updated_at         = Column(DateTime,    default=app_now, onupdate=app_now)

    contact    = relationship("CRMContact",  back_populates="sales_opps")
    quote      = relationship("CRMQuote",    back_populates="opportunity", foreign_keys=[quote_id])
    activities = relationship("CRMActivity", foreign_keys="CRMActivity.deal_id",
                              primaryjoin="and_(CRMActivity.deal_id == CRMSalesOpportunity.id, "
                                          "CRMActivity.deal_type == 'sales')",
                              lazy="select", overlaps="sourcing_deal_activities")


class CRMQuote(Base):
    """Sales quotation header."""
    __tablename__ = "crm_quotes"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_number    = Column(String(30), unique=True, nullable=False, index=True)  # QT-2025-0001
    contact_id      = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True, index=True)
    quote_date      = Column(Date,        default=app_now)
    valid_until     = Column(Date,        nullable=True)
    payment_terms   = Column(String(200), nullable=True)
    special_conditions = Column(Text,     nullable=True)
    total_amount    = Column(Numeric(14, 2), default=0)
    status          = Column(String(20),  default="draft")  # draft/sent/negotiating/accepted/rejected/expired
    sent_at         = Column(DateTime,    nullable=True)
    created_by      = Column(String(50),  nullable=True)
    created_at      = Column(DateTime,    default=app_now)
    updated_at      = Column(DateTime,    default=app_now, onupdate=app_now)

    # ── Customer Payments (Accounts) — Complete Payment / Upload Invoice ────
    payment_status       = Column(String(20), nullable=False, default="pending")  # pending/paid
    utr_number           = Column(String(100), nullable=True)
    payment_snapshot_path= Column(String(255), nullable=True)
    invoice_path         = Column(String(255), nullable=True)

    contact     = relationship("CRMContact",          back_populates="quotes")
    items       = relationship("CRMQuoteItem",         back_populates="quote",
                               cascade="all, delete-orphan", order_by="CRMQuoteItem.sort_order")
    opportunity = relationship("CRMSalesOpportunity",  back_populates="quote",
                               foreign_keys="CRMSalesOpportunity.quote_id")


class CRMQuoteItem(Base):
    """Individual line item in a sales quote."""
    __tablename__ = "crm_quote_items"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_id      = Column(UUID(as_uuid=True), ForeignKey("crm_quotes.id"), nullable=False, index=True)
    line_number   = Column(Integer,     nullable=False, default=1)
    device_type   = Column(String(100), nullable=False)   # "Laptop HP EliteBook 840 G5"
    material_type = Column(String(30),  nullable=True)
    grade         = Column(String(10),  nullable=True)    # A/B/C/D/AsIs
    # Same master list the PO line items draw on — master_options('po_category').
    # Free-text width (not an enum) because the list is operator-editable from
    # Master Data; constraining it in the DB would break the moment someone
    # adds a category there.
    po_category   = Column(String(100), nullable=True)
    quantity      = Column(Integer,     nullable=False, default=1)
    unit_price    = Column(Numeric(10, 2), nullable=False)
    total_price   = Column(Numeric(14, 2), nullable=False)
    specs_note    = Column(Text,        nullable=True)    # "i5 8th Gen, 8GB, 256SSD"
    sort_order    = Column(Integer,     default=0)

    quote = relationship("CRMQuote", back_populates="items")


class CRMActivity(Base):
    """All interactions: calls, WhatsApp, visits, notes — against contacts or deals."""
    __tablename__ = "crm_activities"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id           = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True, index=True)
    deal_id              = Column(UUID(as_uuid=True), nullable=True, index=True)   # sourcing or sales opp UUID
    deal_type            = Column(String(20), nullable=True)           # sourcing / sales
    activity_type        = Column(String(20), default="call")          # call/whatsapp/visit/email/meeting/note
    direction            = Column(String(10), nullable=True)           # inbound/outbound
    summary              = Column(Text,       nullable=False)
    outcome              = Column(String(30), nullable=True)
    performed_by         = Column(String(50), nullable=False)
    activity_date        = Column(DateTime,   default=app_now)
    next_followup        = Column(DateTime,   nullable=True, index=True)
    followup_assigned_to = Column(String(50), nullable=True)
    followup_done        = Column(Boolean,    default=False, index=True)
    created_at           = Column(DateTime,   default=app_now)

    contact = relationship("CRMContact", back_populates="activities")


class GradePriceMatrix(Base):
    """Admin-configurable buy/sell price benchmarks by device type + grade + material."""
    __tablename__ = "crm_grade_price_matrix"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_type = Column(String(100), nullable=False, index=True)  # Laptop, Desktop, Monitor…
    grade       = Column(String(10),  nullable=False)              # A/B/C/D/Mix/Scrap
    material_type = Column(String(30), nullable=True)              # as_is_tested, refurb_full…
    brand       = Column(String(50),  nullable=True)               # optional brand filter

    # Price benchmarks (₹ per unit)
    min_buy_price  = Column(Numeric(10, 2), nullable=True)   # floor buying price
    max_buy_price  = Column(Numeric(10, 2), nullable=True)   # ceiling buying price
    target_sell    = Column(Numeric(10, 2), nullable=True)   # expected sale price
    min_margin_pct = Column(Numeric(5, 2),  default=15.0)    # minimum acceptable margin %

    notes      = Column(Text,       nullable=True)
    updated_by = Column(String(50), nullable=True)
    updated_at = Column(DateTime,   default=app_now, onupdate=app_now)
    created_at = Column(DateTime,   default=app_now)


class CRMPurchaseOrder(Base):
    """Formal PO sent to supplier, linked from sourcing deal."""
    __tablename__ = "crm_purchase_orders"

    id                    = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    po_number             = Column(String(30),  unique=True, nullable=False, index=True)  # PO-2025-0001
    deal_id               = Column(UUID(as_uuid=True), ForeignKey("crm_sourcing_deals.id"), nullable=True)
    contact_id            = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True, index=True)
    po_date               = Column(Date,         nullable=False, default=app_now)
    expected_delivery_date= Column(Date,         nullable=True)
    supplier_gstin        = Column(String(20),   nullable=True)
    delivery_address      = Column(Text,         nullable=True)
    delivery_city         = Column(String(100),  nullable=True)
    delivery_state        = Column(String(100),  nullable=True)
    delivery_pincode      = Column(String(10),   nullable=True)
    payment_terms         = Column(Text,         nullable=True)
    advance_amount        = Column(Numeric(14,2), nullable=True)
    total_amount          = Column(Numeric(14,2), default=0)      # sum of line items (grand total)
    offer_total           = Column(Numeric(14,2), nullable=True)  # our offer price (manually entered)
    status                = Column(String(20),   default="draft")  # draft/issued/acknowledged/received/cancelled
    issued_by             = Column(String(50),   nullable=True)
    issued_at             = Column(DateTime,     nullable=True)
    notes                 = Column(Text,         nullable=True)
    created_by            = Column(String(50),   nullable=True)
    created_at            = Column(DateTime,     default=app_now)
    updated_at            = Column(DateTime,     default=app_now, onupdate=app_now)

    # ── Supplier Payments (Accounts) — Complete Payment / Upload Invoice ────
    payment_status        = Column(String(20),   nullable=False, default="pending")  # pending/paid
    utr_number            = Column(String(100),  nullable=True)
    payment_snapshot_path = Column(String(255),  nullable=True)
    invoice_path          = Column(String(255),  nullable=True)

    contact    = relationship("CRMContact",       foreign_keys=[contact_id])
    deal       = relationship("CRMSourcingDeal",  foreign_keys=[deal_id])
    line_items = relationship("CRMPOLineItem",    back_populates="po",
                              cascade="all, delete-orphan", order_by="CRMPOLineItem.sort_order")


class CRMSourcingDealLineItem(Base):
    """Draft PO line items accumulated on a Sourcing Deal (via 'Add PO Items').
    Copied into CRMPOLineItem when a PO is generated from the deal. Mirrors the
    CRMPOLineItem shape."""
    __tablename__ = "crm_sourcing_deal_line_items"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deal_id     = Column(UUID(as_uuid=True), ForeignKey("crm_sourcing_deals.id"), nullable=False, index=True)
    item_name   = Column(String(200), nullable=False)
    description = Column(Text,         nullable=True)
    po_category = Column(String(100),  nullable=True)
    lot_number  = Column(String(50),   nullable=True)
    device_type = Column(String(100),  nullable=True)
    grade       = Column(String(10),   nullable=True)
    quantity    = Column(Integer,      nullable=False, default=1)
    unit_price  = Column(Numeric(10, 2), nullable=False, default=0)
    total_price = Column(Numeric(14, 2), nullable=False, default=0)
    offer_price = Column(Numeric(14, 2), nullable=True)   # our negotiated offer for this line, set via the Offer Price modal
    sort_order  = Column(Integer,      default=0)

    deal = relationship("CRMSourcingDeal", back_populates="po_line_items")


class CRMPOLineItem(Base):
    __tablename__ = "crm_po_line_items"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    po_id       = Column(UUID(as_uuid=True), ForeignKey("crm_purchase_orders.id"), nullable=False)
    item_name   = Column(String(200), nullable=True)
    description = Column(String(300), nullable=True)
    po_category = Column(String(100), nullable=True)
    lot_number  = Column(String(50),  nullable=True)
    device_type = Column(String(100), nullable=True)
    grade       = Column(String(10),  nullable=True)
    quantity    = Column(Integer,     nullable=False, default=1)
    unit_price  = Column(Numeric(10,2), nullable=False)
    total_price = Column(Numeric(14,2), nullable=False)
    sort_order  = Column(Integer,     default=0)

    po = relationship("CRMPurchaseOrder", back_populates="line_items")


class SupplierPayment(Base):
    """Payment made to a supplier against a lot or PO."""
    __tablename__ = "supplier_payments"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id    = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True)
    lot_id        = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=True)
    po_id         = Column(UUID(as_uuid=True), ForeignKey("crm_purchase_orders.id"), nullable=True)
    sourcing_deal_id = Column(UUID(as_uuid=True), ForeignKey("crm_sourcing_deals.id"), nullable=True)
    payment_date  = Column(Date, nullable=False, default=date.today)
    amount        = Column(Numeric(14, 2), nullable=False)
    payment_mode  = Column(String(20), nullable=True)   # cash/upi/neft/cheque/rtgs
    reference_no  = Column(String(100), nullable=True)  # UTR / cheque no
    is_advance    = Column(Boolean, default=False)
    notes         = Column(Text, nullable=True)
    invoice_path       = Column(String(255), nullable=True)
    payment_photo_path = Column(String(255), nullable=True)
    created_by    = Column(String(50), nullable=True)
    created_at    = Column(DateTime, default=app_now)

    contact = relationship("CRMContact", foreign_keys=[contact_id])
    lot     = relationship("Lot",        foreign_keys=[lot_id])
    sourcing_deal = relationship("CRMSourcingDeal", foreign_keys=[sourcing_deal_id])


class CustomerReceipt(Base):
    """Payment received from a buyer against a sale or dealer order."""
    __tablename__ = "customer_receipts"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id       = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"), nullable=True)
    dealer_id        = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=True)
    sale_id          = Column(UUID(as_uuid=True), ForeignKey("sales.id"), nullable=True)
    dealer_order_id  = Column(UUID(as_uuid=True), ForeignKey("dealer_orders.id"), nullable=True)
    opportunity_id   = Column(UUID(as_uuid=True), ForeignKey("crm_sales_opportunities.id"), nullable=True)
    receipt_date     = Column(Date, nullable=False, default=date.today)
    amount           = Column(Numeric(14, 2), nullable=False)
    payment_mode     = Column(String(20), nullable=True)
    reference_no     = Column(String(100), nullable=True)
    notes            = Column(Text, nullable=True)
    invoice_path       = Column(String(255), nullable=True)
    payment_photo_path = Column(String(255), nullable=True)
    created_by       = Column(String(50), nullable=True)
    created_at       = Column(DateTime, default=app_now)

    contact = relationship("CRMContact", foreign_keys=[contact_id])
    dealer  = relationship("Dealer",     foreign_keys=[dealer_id])
    opportunity = relationship("CRMSalesOpportunity", foreign_keys=[opportunity_id])


# ── ASSIGN LEADS MODULE ───────────────────────────────────────────────────────
LEAD_PLATFORMS = [
    "Facebook", "Instagram", "Google Ads", "LinkedIn",
    "JustDial", "Indiamart", "WhatsApp", "OLX", "Amazon", "Other",
]
LEAD_CONTACT_MODES = ["Phone Call", "WhatsApp", "Email", "In-Person", "Video Call", "SMS"]
LEAD_DEVICE_CATEGORIES = ["Laptop", "Desktop", "Monitor", "Mini PC"]
LEAD_DEALING_GRADES = ["Grade A", "Grade B", "Grade C", "Lot"]
LEAD_WHOM_TO_SELL = ["Corporate", "Retail", "End User"]
LEAD_DEALS_IN = ["Indian", "Imported", "Both"]


class CRMLeadGroup(Base):
    """Ad-campaign / lead-group accordion headers for Assign Leads."""
    __tablename__ = "crm_lead_groups"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name       = Column(String(200), nullable=False, index=True)
    created_by = Column(String(50),  nullable=False)
    created_at = Column(DateTime,    default=app_now)
    updated_at = Column(DateTime,    default=app_now, onupdate=app_now)

    leads = relationship("CRMLead", back_populates="group", lazy="select")


class CRMLead(Base):
    """Individual prospect inside a lead group."""
    __tablename__ = "crm_leads"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id           = Column(String(20),  unique=True, nullable=False, index=True)  # 12-digit
    group_id          = Column(UUID(as_uuid=True), ForeignKey("crm_lead_groups.id"), nullable=False, index=True)
    lead_date         = Column(Date,        nullable=True)
    platform          = Column(String(100), nullable=True)
    device_categories = Column(Text,        nullable=True)   # JSON array e.g. '["Laptop","Monitor"]'
    purchase_quantity = Column(String(50),  nullable=True)   # was units_expected (Integer) — renamed + freeform
    planning_to_buy   = Column(String(200), nullable=True)
    contact_mode      = Column(String(50),  nullable=True)
    name              = Column(String(200), nullable=True)
    phone             = Column(String(30),  nullable=True)
    email             = Column(String(150), nullable=True)
    address           = Column(Text,        nullable=True)
    call_status       = Column(String(50),  nullable=True)   # last call outcome
    full_remark       = Column(Text,        nullable=True)   # deprecated — call log's full_remarks is now shown instead
    assigned_to       = Column(String(50),  nullable=True, index=True)
    selling_quantity  = Column(String(50),  nullable=True)
    whom_to_sell      = Column(String(30),  nullable=True)   # Corporate, Retail, End User
    deals_in          = Column(String(20),  nullable=True)   # Indian, Imported, Both
    dealing_grades    = Column(Text,        nullable=True)   # JSON array e.g. '["Grade A","Lot"]'
    created_by        = Column(String(50),  nullable=False)
    created_at        = Column(DateTime,    default=app_now)
    updated_at        = Column(DateTime,    default=app_now, onupdate=app_now)

    group = relationship("CRMLeadGroup", back_populates="leads")
    calls = relationship("CRMLeadCall",  back_populates="lead",  lazy="select")


class CRMLeadCall(Base):
    """Call log entry for a lead (Assign Leads module)."""
    __tablename__ = "crm_lead_calls"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id           = Column(UUID(as_uuid=True), ForeignKey("crm_leads.id"), nullable=False, index=True)
    calling_date      = Column(Date,        nullable=False)
    followup_date     = Column(Date,        nullable=True)
    outcome           = Column(String(50),  nullable=True)   # shown to users as "Status"
    device_categories = Column(Text,        nullable=True)   # JSON array
    quantity          = Column(String(50),  nullable=True)   # freeform (was Integer)
    full_remarks      = Column(Text,        nullable=True)
    purchase_quantity = Column(String(50),  nullable=True)
    selling_quantity  = Column(String(50),  nullable=True)
    whom_to_sell      = Column(String(30),  nullable=True)   # Corporate, Retail, End User
    deals_in          = Column(String(20),  nullable=True)   # Indian, Imported, Both
    logged_by         = Column(String(50),  nullable=False)
    created_at        = Column(DateTime,    default=app_now)

    lead = relationship("CRMLead", back_populates="calls")
