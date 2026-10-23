import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Date, ForeignKey, Numeric, Integer, Boolean, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class ScrapSale(Base):
    """One Custom Sale of scrap, entered from the Scrap for Sale page's
    "Custom Sale" modal (2026-10-16) and listed on the Scrap Sale Report page.

    Stand-alone record: it does not touch any Tag Number or Scrap for Sale
    batch, and is deliberately NOT counted in Business P&L. The selling
    entity's Company Setting details are snapshotted as columns at save time
    (company_id kept for traceability) so later edits to Company Settings never
    change a past record. total_sale_price is always recomputed server-side."""
    __tablename__ = "scrap_sales"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Scrap ID batch this sale sold, when made from a batch's Sell (2026-10-23,
    # schema change approved: "I approve dbdiagram"). NULL for a Custom Sale.
    scrap_for_sale_id = Column(UUID(as_uuid=True), ForeignKey("scrap_for_sale.id"), nullable=True, index=True)
    sale_number = Column(String(20), unique=True, nullable=False, index=True)  # SCS-0001
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)

    # Entity snapshot (Row 0)
    entity = Column(String(50), nullable=True)
    entity_name = Column(String(200), nullable=True)
    entity_address = Column(String(500), nullable=True)
    entity_gstin = Column(String(20), nullable=True)
    entity_state = Column(String(100), nullable=True)
    entity_phone = Column(String(50), nullable=True)
    entity_email = Column(String(100), nullable=True)

    # Row 1
    sale_date = Column(Date, nullable=False)
    sales_person = Column(String(100), nullable=True)
    invoice_number = Column(String(50), nullable=True)

    # Rows 2-3: customer
    customer_company = Column(String(200), nullable=False)
    customer_phone = Column(String(50), nullable=True)
    customer_email = Column(String(100), nullable=True)
    customer_address = Column(String(500), nullable=True)
    customer_city = Column(String(100), nullable=True)
    customer_state = Column(String(100), nullable=True)

    total_sale_price = Column(Numeric(14, 2), nullable=False, default=0)
    is_trashed = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)

    items = relationship("ScrapSaleItem", back_populates="sale",
                         cascade="all, delete-orphan", order_by="ScrapSaleItem.line_no",
                         lazy="selectin")
    tags = relationship("ScrapSaleTag", back_populates="sale",
                        cascade="all, delete-orphan", lazy="selectin")


class ScrapSaleItem(Base):
    """One item row of a ScrapSale. Priced either by weight
    (total_weight x unit_price_weight) or by count (quantity x unit_price) —
    pricing_basis records which; total_price is computed server-side."""
    __tablename__ = "scrap_sale_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scrap_sale_id = Column(UUID(as_uuid=True), ForeignKey("scrap_sales.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    line_no = Column(Integer, nullable=False)
    item_name = Column(String(200), nullable=False)
    item_category = Column(String(200), nullable=True)  # Master Data "Device Sub-Categories" (sub_category)
    total_weight = Column(Numeric(12, 3), nullable=True)
    unit_price_weight = Column(Numeric(12, 2), nullable=True)
    quantity = Column(Integer, nullable=True)
    unit_price = Column(Numeric(12, 2), nullable=True)
    pricing_basis = Column(String(10), nullable=False)  # weight | quantity
    total_price = Column(Numeric(14, 2), nullable=False, default=0)

    sale = relationship("ScrapSale", back_populates="items")


class ScrapSaleTag(Base):
    """A Tag Number sold in a ScrapSale (2026-10-23, schema change approved:
    "I approve dbdiagram"). Written when the sale is made from a Scrap ID
    batch's Sell or from Tags Scrapped's Sell; the tag moves to Sold then."""
    __tablename__ = "scrap_sale_tags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scrap_sale_id = Column(UUID(as_uuid=True), ForeignKey("scrap_sales.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    sale = relationship("ScrapSale", back_populates="tags")
