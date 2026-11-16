import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class PartRequest(Base):
    """A spare-part request raised by an L1/L2/L3 engineer from the device
    Parts Consumption section. Actioned by the Spare Parts Manager on the
    Part Master page (Handover / Not In Stock / Procure)."""
    __tablename__ = "part_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    work_order_id = Column(UUID(as_uuid=True), ForeignKey("work_orders.id"), nullable=True)
    work_id = Column(String(12), nullable=True)            # engineer WorkID snapshot
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    barcode = Column(String(100), nullable=True)
    stage = Column(String(5), nullable=True)               # l1 | l2 | l3 (page that raised it)

    part_id = Column(UUID(as_uuid=True), ForeignKey("spare_parts.id"), nullable=True)
    part_name = Column(String(150), nullable=False)        # fixed-list label snapshot
    part_category = Column(String(100), nullable=True)     # selected in the New Request/Replace modal
    part_capacity = Column(String(50), nullable=True)      # e.g. 8GB, 256GB
    part_type     = Column(String(50), nullable=True)      # e.g. DDR4, SSD
    request_type = Column(String(20), nullable=False, default="new")  # new | replace | downgrade
    # normal | replace — only set when request_type == "downgrade" (Device Detail Downgrade modal)
    downgrade_type = Column(String(20), nullable=True)
    part_make = Column(String(100), nullable=True)   # Downgrade modal, Multi Request
    part_model = Column(String(100), nullable=True)  # Downgrade modal, Multi Request

    requested_by = Column(String(50), nullable=True)       # engineer username
    engineer_name = Column(String(100), nullable=True)

    qty_requested = Column(Integer, nullable=False, default=1)
    qty_handed_over = Column(Integer, nullable=False, default=0)

    # requested | handed_over | not_in_stock | procure | scrapped
    status = Column(String(20), nullable=False, default="requested", index=True)

    created_at = Column(DateTime, default=app_now)
    actioned_at = Column(DateTime, nullable=True)
    actioned_by = Column(String(50), nullable=True)

    device = relationship("Device", lazy="select")
    part = relationship("SparePart", lazy="select")


class PartSourcingRequest(Base):
    """Raised when the Spare Parts Manager clicks 'Procure' on a part request.
    Appears in the CRM Dashboard 'Pending Requests For Part Sourcing' (Sales
    Manager closes the deal) and mirrored read-only on the Part Master page."""
    __tablename__ = "part_sourcing_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    part_request_id = Column(UUID(as_uuid=True), ForeignKey("part_requests.id"), nullable=True)
    part_id = Column(UUID(as_uuid=True), ForeignKey("spare_parts.id"), nullable=True)
    part_code = Column(String(20), nullable=True)
    part_name = Column(String(150), nullable=False)

    qty_requested = Column(Integer, nullable=False, default=1)
    qty_sourced = Column(Integer, nullable=False, default=0)

    raised_by = Column(String(50), nullable=True)          # spare parts manager username
    status = Column(String(10), nullable=False, default="open", index=True)  # open | closed
    source_deal_id = Column(String(50), nullable=True)     # sourcing deal UUID string, set at Close Deal

    created_at = Column(DateTime, default=app_now)
    closed_at = Column(DateTime, nullable=True)
    closed_by = Column(String(50), nullable=True)

    # Verify step (Part Master, Sourcing Requests tab) — independent of Close Deal
    verified = Column(Boolean, nullable=False, default=False, server_default="false")
    verified_at = Column(DateTime, nullable=True)
    verified_by = Column(String(50), nullable=True)

    # Send to Reupload — from the Verify Sourcing modal, asks the uploader to
    # re-submit documents. Does not change verified/status; the Sourcing
    # Requests table's Action column intentionally stays unchanged.
    reupload_requested = Column(Boolean, nullable=False, default=False, server_default="false")
    reupload_requested_at = Column(DateTime, nullable=True)
    reupload_requested_by = Column(String(50), nullable=True)

    # ── Production-raised requests (Part Estimation page) ────────────────────
    # 'procure'    — the original flow: Spare Parts Manager clicked Procure on a
    #                single engineer part request, closed by Sales in the CRM.
    # 'production' — raised by Download & Attach Estimate on Part Estimation.
    #                Whole-lot request; Status renders as "Production", Part Code
    #                stays blank, Part Name links back to the lot's estimate, and
    #                the Action column offers Confirm Request instead of Verify.
    # server_default is emitted verbatim into ALTER TABLE ... DEFAULT <x> by
    # db_validator, so the quotes are part of the value — without them Postgres
    # reads 'procure' as a column reference and the whole migration aborts.
    source = Column(String(20), nullable=False, default="procure", server_default="'procure'", index=True)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=True)
    lot_number = Column(String(100), nullable=True)
    estimate_id = Column(UUID(as_uuid=True), ForeignKey("part_estimates.id"), nullable=True)

    confirmed = Column(Boolean, nullable=False, default=False, server_default="false")
    confirmed_at = Column(DateTime, nullable=True)
    confirmed_by = Column(String(50), nullable=True)

    # Activity Board review (2026-11-15, Pankaj — approved): a new request is
    # "pending" until someone verifies or rejects it on Activity Board;
    # Procurement Board only offers its actions once it's "verified". NULL =
    # raised before this existed = treated as verified.
    review_status = Column(String(15), nullable=True, default="pending")
    review_note   = Column(Text, nullable=True)
    reviewed_by   = Column(String(50), nullable=True)
    reviewed_at   = Column(DateTime, nullable=True)

