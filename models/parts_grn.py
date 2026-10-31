import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Numeric, Integer, Text, Boolean, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class PartsGRN(Base):
    __tablename__ = "parts_grn"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grn_number = Column(String(20), unique=True, nullable=False, index=True)

    po_number = Column(String(50), nullable=True)
    po_date = Column(DateTime, nullable=True)
    po_file = Column(String(255), nullable=True)

    invoice_number = Column(String(50), nullable=True)
    invoice_date = Column(DateTime, nullable=True)
    invoice_file = Column(String(255), nullable=True)

    eway_bill_number = Column(String(50), nullable=True)
    eway_bill_date = Column(DateTime, nullable=True)
    eway_bill_file = Column(String(255), nullable=True)

    vehicle_number = Column(String(50), nullable=True)
    vehicle_seal_file = Column(String(255), nullable=True)
    vehicle_image_file = Column(String(255), nullable=True)

    # ── Admin Dashboard analytics: GRN in Plan vs in TRC ──────────────────────
    status = Column(String(20), nullable=False, default="in_plan", server_default="in_plan")   # in_plan / in_trc

    date_received = Column(DateTime, nullable=True)
    vendor_name = Column(String(150), nullable=True)

    invoice_value = Column(Numeric(12, 2), nullable=True)
    sgst = Column(Numeric(10, 2), nullable=True)
    cgst = Column(Numeric(10, 2), nullable=True)
    igst = Column(Numeric(10, 2), nullable=True)
    tax_amount = Column(Numeric(10, 2), nullable=True)

    total_po_qty = Column(Integer, nullable=True)
    total_invoice_qty = Column(Integer, nullable=True)
    total_physical_qty = Column(Integer, nullable=True)
    total_amount_invoice = Column(Numeric(12, 2), nullable=True)

    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    is_trashed = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    trashed_at = Column(DateTime, nullable=True)

    line_items = relationship("PartsGRNLineItem", back_populates="grn", lazy="select")


class PartsGRNLineItem(Base):
    __tablename__ = "parts_grn_line_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grn_id = Column(UUID(as_uuid=True), ForeignKey("parts_grn.id"), nullable=False, index=True)

    part_id = Column(String(8), nullable=False, index=True)
    lot_number = Column(String(50), nullable=True)
    po_number = Column(String(50), nullable=True)
    grn_number = Column(String(20), nullable=True)
    vendor_name = Column(String(150), nullable=True)
    invoice_number = Column(String(50), nullable=True)
    product_description = Column(Text, nullable=True)
    item_name = Column(String(150), nullable=True)
    part_brand = Column(String(100), nullable=True)
    part_model = Column(String(100), nullable=True)
    part_name = Column(String(150), nullable=True)
    invoice_qty = Column(Integer, nullable=True)
    physical_qty = Column(Integer, nullable=True)
    price = Column(Numeric(10, 2), nullable=True)
    main_category = Column(String(50), nullable=True)
    category = Column(String(50), nullable=True)
    vehicle_number = Column(String(50), nullable=True)
    invoice_ref = Column(String(255), nullable=True)
    remarks = Column(Text, nullable=True)
    is_harvest = Column(Boolean, default=False, nullable=False)
    # Crate the received qty was put in (2026-10-31): posting adds to the
    # Part Master row for Part Name + Make + Model + Type + this crate.
    crate_number = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=app_now)

    grn = relationship("PartsGRN", back_populates="line_items")
