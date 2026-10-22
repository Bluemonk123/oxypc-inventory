"""Rentals (2026-10-22, approved via dbdiagram).

RentalRecord: one rental — billing entity (Company Setting, details copied at
submit), Billing To (Business / Individual customer), duration, totals.
RentalItem: one row per rented tag, its details copied at submit.

Submitting moves the tags Ready to Sale -> On Rent; Mark Returned / Cancel
moves them back. Totals are computed on the server (routers/rentals.py).
"""
import uuid

from sqlalchemy import (Column, String, DateTime, Date, Integer, Numeric, Text,
                        ForeignKey, UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID

from database import Base
from utils.timezone import app_now

RENTAL_STATUSES = [("active", "Active"), ("returned", "Returned"), ("cancelled", "Cancelled")]
DURATION_UNITS = [("days", "Days"), ("months", "Months"), ("years", "Years")]
BILL_TYPES = [("business", "Business"), ("individual", "Individual")]


class RentalRecord(Base):
    __tablename__ = "rental_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rental_number = Column(String(20), unique=True, nullable=False, index=True)   # RNT-2026-0001
    status = Column(String(20), nullable=False, default="active", index=True)

    billing_company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
    billing_entity_name = Column(String(200), nullable=True)
    billing_entity_address = Column(String(500), nullable=True)
    billing_entity_gstin = Column(String(20), nullable=True)
    billing_entity_state = Column(String(100), nullable=True)
    billing_entity_phone = Column(String(50), nullable=True)
    billing_entity_email = Column(String(100), nullable=True)

    bill_type = Column(String(20), nullable=False)          # business | individual
    customer_company = Column(String(200), nullable=True)   # Business only
    customer_gstin = Column(String(20), nullable=True)      # Business only
    person_name = Column(String(150), nullable=False)
    phone = Column(String(20), nullable=True)
    email = Column(String(150), nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    address = Column(Text, nullable=True)

    duration_value = Column(Integer, nullable=False)
    duration_unit = Column(String(10), nullable=False)      # days | months | years
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)

    total_quantity = Column(Integer, nullable=False)
    suggested_price = Column(Numeric(14, 2), nullable=True)   # sum of tags' Min Selling Price at submit
    total_price = Column(Numeric(14, 2), nullable=False)
    notes = Column(Text, nullable=True)

    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, nullable=False, default=app_now)
    closed_at = Column(DateTime, nullable=True)
    closed_by = Column(String(50), nullable=True)


class RentalItem(Base):
    __tablename__ = "rental_items"
    __table_args__ = (UniqueConstraint("rental_id", "device_id", name="uq_rental_items_rental_device"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rental_id = Column(UUID(as_uuid=True), ForeignKey("rental_records.id"), nullable=False, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    barcode = Column(String(100), nullable=False)
    make = Column(String(100), nullable=True)
    model = Column(String(150), nullable=True)
    cpu = Column(String(100), nullable=True)
    generation = Column(String(50), nullable=True)
    ram = Column(String(50), nullable=True)
    storage = Column(String(50), nullable=True)
    grade = Column(String(10), nullable=True)
    min_selling_price = Column(Numeric(14, 2), nullable=True)
