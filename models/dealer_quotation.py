import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Numeric, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class DealerQuotation(Base):
    """Quotation shared with a dealer — snapshots dealer + company details at
    creation time so the document stays accurate even if the Dealer record or
    Company Settings change later."""
    __tablename__ = "dealer_quotations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quote_number = Column(String(30), unique=True, nullable=False, index=True)  # QTN-0001
    dealer_id = Column(UUID(as_uuid=True), ForeignKey("dealers.id"), nullable=False, index=True)

    # Dealer snapshot (for the "Quotation Shared" table + document header)
    customer_name = Column(String(200), nullable=False)
    customer_type = Column(String(30), nullable=True)
    dealer_phone = Column(String(20), nullable=True)
    dealer_email = Column(String(100), nullable=True)
    dealer_address = Column(Text, nullable=True)
    dealer_gstin = Column(String(20), nullable=True)

    # Company snapshot (from Company Settings at creation time)
    company_name = Column(String(200), nullable=True)
    company_address = Column(Text, nullable=True)
    company_gstin = Column(String(20), nullable=True)
    company_phone = Column(String(20), nullable=True)
    company_email = Column(String(100), nullable=True)

    payment_terms = Column(Text, nullable=True)
    additional_notes = Column(Text, nullable=True)

    total_quantity = Column(Integer, nullable=False, default=0)
    total_price = Column(Numeric(14, 2), nullable=False, default=0)

    status = Column(String(20), nullable=False, default="added")  # added / negotiation / approved / rejected / expired (2026-11-10; was shared / confirmed)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)
    confirmed_by = Column(String(50), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)

    dealer = relationship("Dealer")
    items = relationship("DealerQuotationItem", back_populates="quotation",
                          cascade="all, delete-orphan", order_by="DealerQuotationItem.sort_order")


class DealerQuotationItem(Base):
    __tablename__ = "dealer_quotation_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quotation_id = Column(UUID(as_uuid=True), ForeignKey("dealer_quotations.id"), nullable=False, index=True)
    po_category = Column(String(100), nullable=True)  # from master_options('po_category')
    product_name = Column(String(200), nullable=False)
    model_name = Column(String(200), nullable=True)
    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    total_price = Column(Numeric(14, 2), nullable=False, default=0)
    sort_order = Column(Integer, default=0)

    quotation = relationship("DealerQuotation", back_populates="items")
