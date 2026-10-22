import uuid
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base



class Sale(Base):
    __tablename__ = "sales"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sale_number = Column(String(20), unique=True, nullable=False, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    sale_price = Column(Numeric(12, 2), nullable=False)
    customer_name = Column(String(100), nullable=True)
    customer_phone = Column(String(20), nullable=True)
    customer_state = Column(String(100), nullable=True)  # For GST state determination (intra/inter)
    customer_address = Column(Text, nullable=True)
    invoice_no = Column(String(50), nullable=True)
    # Widened from 20: Payment Mode now sources from Master Data's Dropdown
    # Configuration (payment_mode category, e.g. "Bank Transfer (NEFT/RTGS/IMPS)"
    # at 30 chars), not the old hardcoded cash/upi/card/credit codes.
    payment_mode = Column(String(50), nullable=True)
    sold_by = Column(String(50), nullable=True, index=True)
    # Who gets CREDIT for the sale, which is not the same thing as sold_by —
    # sold_by records the logged-in operator who keyed the entry. Free text
    # rather than a user FK because the people credited (floor staff, walk-in
    # counter) do not all have platform logins.
    sales_person = Column(String(100), nullable=True, index=True)
    sold_at = Column(DateTime, default=app_now)
    notes = Column(Text, nullable=True)
    # ── Transport ────────────────────────────────────────────────────────────────
    payment_reference = Column(String(100), nullable=True)   # cheque no / UTR / NEFT ref
    transport_mode    = Column(String(30), nullable=True)    # courier / hand_delivery / self_pickup
    transport_via     = Column(String(100), nullable=True)   # courier company name
    tracking_number   = Column(String(100), nullable=True)   # AWB / tracking number
    dispatch_date     = Column(DateTime, nullable=True)      # when dispatched
    delivery_status   = Column(String(30), nullable=True)    # pending / dispatched / delivered
    # ── Invoice / PO upload ──────────────────────────────────────────────────────
    invoice_file_path = Column(String(500), nullable=True)   # relative path to uploaded PDF
    # ── Warranty at sale (Phase 1a) ──────────────────────────────────────────────
    warranty_type       = Column(String(20), default="none")   # none/30_days/6_months/1_year
    warranty_expires_at = Column(DateTime, nullable=True)       # server-computed from sold_at + duration
    # ── Extended Warranty page (2026-09-14) ───────────────────────────────────────
    # Running total of warranty length in days. Set from Warranty Type's duration
    # when (re)assigned via the Extended Warranty page's top form; incremented by
    # the Update modal's "Extended Warranty" date whenever a warranty is extended
    # past its current Warranty Stops date. Nullable — sales that have never had
    # their warranty touched by this page fall back to WARRANTY_DURATIONS[warranty_type]
    # for display (see routers/extended_warranty.py).
    warranty_days = Column(Integer, nullable=True)
    # ── Sales channel (Admin Dashboard analytics) ─────────────────────────────
    sale_channel = Column(String(20), nullable=True)   # procurement / telecaller / showroom
    # ── Selling company, resolved and SNAPSHOTTED at sale time (2026-08) ──────
    # Matched from the sold device's entity to the Company Setting row tagged
    # with that same entity. company_id is kept for traceability/joins, but
    # the invoice-relevant fields are copied here too — a genuine snapshot,
    # not just a foreign key — because a foreign key alone would still change
    # if someone later edits THAT SAME company's name/GSTIN/address rather
    # than switching companies. A Tax Invoice/Delivery Challan is a legal
    # document: it must always show the company exactly as it was on the sale
    # date, regardless of edits or deactivation afterward. Nullable: sales
    # recorded before this column existed have no snapshot and print.py/
    # waybill.py fall back to the pre-existing "oldest active company" live
    # lookup for those (see get_company_settings).
    company_id           = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
    company_name         = Column(String(200), nullable=True)
    company_address      = Column(String(500), nullable=True)
    company_gstin        = Column(String(20), nullable=True)
    company_state        = Column(String(100), nullable=True)
    company_state_code   = Column(String(5), nullable=True)
    company_phone        = Column(String(50), nullable=True)
    company_email        = Column(String(100), nullable=True)

    device = relationship("Device", back_populates="sales")
    returns = relationship("Return", back_populates="sale", lazy="select")


class Return(Base):
    __tablename__ = "returns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Optional since 2026-10-22 (approved dbdiagram change): a Credit Note
    # return gets its sale only at "Payment Invoice Done", not on entry.
    sale_id = Column(UUID(as_uuid=True), ForeignKey("sales.id"), nullable=True, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    return_date         = Column(DateTime, default=app_now)
    reason              = Column(Text, nullable=True)
    condition_on_return = Column(String(50), nullable=True)
    action_taken        = Column(String(30), nullable=True)   # restock / scrap / credit
    reentered_stage     = Column(String(50), nullable=True)   # iqc (default)
    processed_by        = Column(String(50), nullable=True)
    refund_amount       = Column(Numeric(12, 2), nullable=True)
    notes               = Column(Text, nullable=True)
    # Approval workflow (migration: 20260515_1000)
    approval_status     = Column(String(20), nullable=True, default='pending')   # pending/approved/rejected
    approved_by         = Column(String(50), nullable=True)
    approved_at         = Column(DateTime, nullable=True)
    rejection_reason    = Column(Text, nullable=True)
    # RMA capture (Phase 1b)
    return_type         = Column(String(20), default="customer")   # customer/dealer
    serial_captured     = Column(String(100), nullable=True)       # serial/barcode scanned at RMA time
    warranty_status     = Column(String(20), nullable=True)        # in_warranty/out_of_warranty/no_warranty (server-computed)
    complaint_text      = Column(Text, nullable=True)               # RMA complaint/issue description
    # Inventory Manager's "Return Stock" table (Verify action) — captured once
    # the returned tag has actually been repaired, distinct from refund_amount
    # above (what the CUSTOMER got back) and from Part Cost (derived on read
    # from SparePartConsumption, not stored here).
    repair_cost         = Column(Numeric(12, 2), nullable=True)
    labour_cost         = Column(Numeric(12, 2), nullable=True)
    # Receipt (Product Return list "Receipt" column / External Tag form) —
    # no customer_email column exists on Sale, and adding one there would
    # touch the whole New Tag Sale flow; scoped here since it's only ever
    # used for the return receipt.
    customer_email      = Column(String(100), nullable=True)
    # ── Credit Note tab (Return New page, 2026-09-22) ─────────────────────
    # cn_number: action_taken == "credit" rows only, from the page's original
    # (since superseded) single-tag CN flow — no longer written by the
    # current Credit Note tab (see debit_note_number below instead).
    # customer_name/phone/email now double as "Sender Details" (2026-09-23)
    # for the Credit Note workflow's multi-tag Debit Note flow — bulk-set
    # together with debit_note_number/amount, shown on the Credit Note page
    # as the "Sender" badge. customer_state/address are unused by that flow.
    cn_number            = Column(String(50), nullable=True)
    customer_name        = Column(String(100), nullable=True)
    customer_phone       = Column(String(20), nullable=True)
    customer_state       = Column(String(100), nullable=True)
    customer_address     = Column(Text, nullable=True)
    # ── Credit Note page workflow (2026-09-22) ─────────────────────────────
    # Every Internal Tag return enters at CN_STAGES[0] ("Return Received")
    # regardless of its eventual outcome (repair/replace/credit) — see
    # process_return. The Return New page's Credit Note tab only ever stamps
    # debit_note_number/amount + sender details (customer_name/phone/email
    # above) — it does NOT advance cn_stage itself (2026-09-23 fix). Every
    # stage move, including Return Received -> Debit Note Verified, happens
    # one at a time through the Credit Note page's own Action column.
    # Distinct from Device.tag_return_status, which still separately tracks
    # Replaced-by/Return-for-Repair/CN for other pages.
    cn_stage             = Column(String(30), nullable=True)
    debit_note_number    = Column(String(50), nullable=True)
    debit_note_amount    = Column(Numeric(12, 2), nullable=True)
    payment_invoice      = Column(String(100), nullable=True)

    sale = relationship("Sale", back_populates="returns")


# Credit Note page workflow stages, in order. Every Internal Tag return
# starts at index 0; the Credit Note page's Action column only ever moves
# forward one stage at a time (the Return New page's Credit Note tab no
# longer auto-advances on its own — it only stamps Debit Note/Sender data).
CN_STAGES = [
    "Return Received", "Debit Note Verified", "Verify Payment",
    "Payment Invoice Done", "Restock Pending", "CN Complete",
]

# Action-button label for advancing FROM CN_STAGES[i] to CN_STAGES[i+1] —
# defaults to the resulting stage name, except the first transition, which
# reads as a verb ("Verify Debit Note") rather than repeating the resulting
# stage's own name ("Debit Note Verified").
CN_STAGE_ACTION_LABELS = {0: "Verify Debit Note"}
