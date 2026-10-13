import uuid
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Boolean, Numeric, Integer, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class Dealer(Base):
    __tablename__ = "dealers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dealer_code = Column(String(20), unique=True, nullable=False, index=True)
    business_name = Column(String(200), nullable=False)
    first_name = Column(String(100), nullable=True)   # Contact first name (for WA personalisation)
    last_name = Column(String(100), nullable=True)    # Contact last name
    contact_person = Column(String(100), nullable=True)
    phone = Column(String(100), nullable=True)  # may hold multiple comma/slash/semicolon/pipe-separated numbers
    whatsapp_number = Column(String(20), nullable=True)
    email = Column(String(100), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    pincode = Column(String(10), nullable=True)
    gstin = Column(String(20), nullable=True)
    dealer_type = Column(String(30), default="retail")  # retail, wholesale, corporate, service
    # Finer grouping under Type (e.g. "Optician", "Hospital", "Chartered accountant").
    # Options come from Master Dropdown category "dealer_sub_category".
    dealer_sub_category = Column(String(150), nullable=True)
    website = Column(String(1000), nullable=True)
    # Google Maps place CID (digits only); link = https://maps.google.com/?cid=<cid>.
    # Also the de-duplication key when a Google Maps export is re-uploaded.
    google_cid = Column(String(30), nullable=True)
    credit_limit = Column(Numeric(14, 2), default=0)
    outstanding_amount = Column(Numeric(14, 2), default=0)
    total_purchases = Column(Numeric(14, 2), default=0)
    last_sale_date = Column(DateTime, nullable=True)
    last_sale_amount = Column(Numeric(14, 2), nullable=True)
    preferred_categories = Column(String(200), nullable=True)  # comma-separated: Laptop, Desktop
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="active")  # active, inactive, blacklisted
    assigned_to = Column(String(50), nullable=True)  # sales exec username
    created_by = Column(String(50), nullable=True)
    source     = Column(String(200), nullable=True)   # e.g. "WhatsApp Group: Dealers Delhi"
    added_by   = Column(String(50),  nullable=True)   # username who added this dealer
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    # ── Soft-delete (Trash) — hides dealer from Dealer Management + Assign
    #    Dealer Leads without physically removing rows referenced by orders/
    #    calls/credit-notes. trashed_at IS NULL == live dealer. ──────────────
    trashed_at = Column(DateTime, nullable=True)
    trashed_by = Column(String(50), nullable=True)

    # ── Link back to the CRM Account (Contact Leads) this dealer represents ──
    # Portal logins are provisioned by picking an Account, but every portal FK
    # in the system (bookings, listings, bids) points at dealers.id. Rather than
    # repoint that whole graph, provisioning resolves-or-creates the Dealer for
    # the chosen Account and records the link here. It is what lets the Bids
    # page show account name / contact / category next to a dealer's bid.
    # Nullable: dealers created before this existed, or added directly in Dealer
    # Management, legitimately have no Account behind them.
    crm_contact_id = Column(UUID(as_uuid=True), ForeignKey("crm_contacts.id"),
                            nullable=True, index=True)

    # ── Trade Partner portal (dealer-facing B2B web) ──────────────────────
    portal_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    portal_password_hash = Column(String(200), nullable=True)   # bcrypt; login id = portal_phone
    portal_phone = Column(String(20), nullable=True, unique=True)  # single normalized login phone
    partner_type = Column(String(20), nullable=True)             # dealer, trader, repair_shop
    price_segment = Column(String(30), nullable=False, default="new_dealer", server_default=text("'new_dealer'"))
    sales_owner_username = Column(String(50), nullable=True)     # drives My Desk + WhatsApp buttons
    portal_last_login_at = Column(DateTime, nullable=True)
    portal_token_only = Column(Boolean, nullable=False, default=False, server_default=text("false"))  # penalty ladder step 3
    portal_password_version = Column(Integer, nullable=False, default=1, server_default=text("1"))  # bump to invalidate JWTs

    calls = relationship("DealerCall", back_populates="dealer", lazy="select")
    assignments = relationship("DealerAssignment", back_populates="dealer", lazy="select")


class DealerAssignment(Base):
    __tablename__ = "dealer_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False)
    assigned_to = Column(String(50), nullable=False)  # username of sales exec
    assigned_by = Column(String(50), nullable=False)
    assigned_at = Column(DateTime, default=app_now)
    is_active = Column(Boolean, default=True)
    notes = Column(Text, nullable=True)

    dealer = relationship("Dealer", back_populates="assignments")


class DealerCall(Base):
    __tablename__ = "dealer_calls"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False)
    called_by = Column(String(50), nullable=False)  # username
    call_date = Column(DateTime, default=app_now)
    call_type = Column(String(20), default="outbound")  # outbound, inbound
    call_mode = Column(String(20), default="phone")  # phone, whatsapp, in_person
    duration_mins = Column(Integer, nullable=True)
    call_outcome = Column(String(30), nullable=True)  # interested, not_interested, callback, order_placed, no_answer, followup
    items_discussed = Column(Text, nullable=True)  # what products discussed
    quote_given = Column(Numeric(12, 2), nullable=True)
    next_followup_date = Column(DateTime, nullable=True, index=True)
    whatsapp_sent = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=app_now)

    # ── Telecalling deal-detail fields (2026-07-03) ───────────────────────
    calling_remark   = Column(Text, nullable=True)       # remark captured during the call itself
    category         = Column(String(50), nullable=True)
    product_model    = Column(String(200), nullable=True)  # "Model" in the requirement sheet
    configuration    = Column(String(200), nullable=True)
    qty              = Column(Integer, nullable=True)   # Required Quantity
    purchase_quantity = Column(Integer, nullable=True)  # Purchase Quantity — distinct from Required Quantity
    asking_price     = Column(Numeric(12, 2), nullable=True)
    deal_status      = Column(String(30), nullable=True)  # open, negotiation, won, lost — distinct from call_outcome
    requirements_preferred_config = Column(Text, nullable=True)
    whom_to_sell     = Column(String(30), nullable=True)   # corporate, end_user, retail
    sale_quantity    = Column(Integer, nullable=True)
    deals_in         = Column(String(20), nullable=True)   # indian, imported
    stock_type       = Column(String(20), nullable=True)   # ready, lot
    monthly_quantity = Column(Integer, nullable=True)       # Monthly Quantity (recurring order size)
    assigned_to      = Column(String(50), nullable=True)   # per-deal assignee (may differ from called_by)

    dealer = relationship("Dealer", back_populates="calls")


class DealerOrder(Base):
    __tablename__ = "dealer_orders"
    __table_args__ = (
        Index(
            "ix_dealer_orders_overdue",
            "payment_due_date",
            "due_amount",
            postgresql_where=text("due_amount > 0 AND payment_due_date IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False)
    order_number = Column(String(30), unique=True, nullable=False)
    order_date = Column(DateTime, default=app_now)
    items_description = Column(Text, nullable=True)
    total_amount = Column(Numeric(14, 2), default=0)
    paid_amount = Column(Numeric(14, 2), default=0)
    due_amount = Column(Numeric(14, 2), default=0)
    payment_due_date = Column(DateTime, nullable=True)
    payment_mode = Column(String(20), nullable=True)  # cash, bank, credit, upi
    invoice_number = Column(String(50), nullable=True)
    invoice_sent_whatsapp = Column(Boolean, default=False)
    payment_reminder_sent = Column(Boolean, default=False)
    status = Column(String(20), default="pending")  # pending, confirmed, delivered, cancelled, paid
    notes = Column(Text, nullable=True)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)

    dealer = relationship("Dealer")


class DealerCreditNote(Base):
    """Goods return / credit note issued against a dealer order."""
    __tablename__ = "dealer_credit_notes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    credit_number = Column(String(30), unique=True, nullable=False, index=True)
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("dealer_orders.id"), nullable=True, index=True)
    credit_date = Column(DateTime, default=app_now)
    amount = Column(Numeric(14, 2), nullable=False)
    reason = Column(String(200), nullable=True)
    items_description = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)

    # Application tracking — set when CN balance is applied against an open order
    is_applied = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    applied_at = Column(DateTime, nullable=True)
    applied_to_order_id = Column(UUID(as_uuid=True), ForeignKey("dealer_orders.id"), nullable=True)

    dealer = relationship("Dealer", lazy="select")
    order = relationship("DealerOrder", foreign_keys=[order_id], lazy="select")
    applied_to_order = relationship("DealerOrder", foreign_keys=[applied_to_order_id], lazy="select")
