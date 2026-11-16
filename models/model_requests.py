"""Model/config demand requests — Sales & Telecalling teams request a
CONFIGURATION they need (brand/model, spec, grade, quantity) from the TRC
production team, independent of any specific existing serial number. This is
distinct from TelecallerDispatchRequest, which requests one already-existing
ready-to-sale device by barcode."""
import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from database import Base


class ModelRequest(Base):
    __tablename__ = "model_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # ── Who's asking ──────────────────────────────────────────────────────
    requested_by = Column(String(50), nullable=False)        # username
    requested_by_name = Column(String(100), nullable=True)
    requested_by_role = Column(String(30), nullable=True)     # sales | telecaller | sales_manager

    # ── What they need ────────────────────────────────────────────────────
    sub_category = Column(String(20), nullable=True)          # Laptop / Desktop / TFT
    brand = Column(String(50), nullable=True)
    model = Column(String(100), nullable=True)
    cpu = Column(String(100), nullable=True)
    ram_gb = Column(Integer, nullable=True)
    storage_gb = Column(Integer, nullable=True)
    storage_type = Column(String(20), nullable=True)          # SSD / HDD
    screen_size = Column(String(20), nullable=True)
    grade = Column(String(10), nullable=True)                 # A / B / C / Any
    qty_requested = Column(Integer, nullable=False, default=1)
    notes = Column(Text, nullable=True)

    # ── Fulfilment (actioned by TRC / inventory manager) ──────────────────
    qty_fulfilled = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="open", index=True)
    # open | partially_fulfilled | fulfilled | cancelled
    fulfilled_by = Column(String(50), nullable=True)
    fulfilled_at = Column(DateTime, nullable=True)
    fulfillment_notes = Column(Text, nullable=True)

    # ── Procurement close (Procure Dashboard "Device Sourcing" tab) — a
    # separate closure path from TRC fulfillment above: this links the
    # request to an actual CRM Sourcing Deal instead of allocating existing
    # stock. status="closed" once this happens.
    source_deal_id = Column(String(50), nullable=True)     # sourcing deal UUID string
    sourcing_notes = Column(Text, nullable=True)
    closed_by = Column(String(50), nullable=True)
    closed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    # Activity Board review (2026-11-15, Pankaj — approved): a new request is
    # "pending" until someone verifies or rejects it on Activity Board;
    # Procurement Board only offers its actions once it's "verified". NULL =
    # raised before this existed = treated as verified.
    review_status = Column(String(15), nullable=True, default="pending")
    review_note   = Column(Text, nullable=True)
    reviewed_by   = Column(String(50), nullable=True)
    reviewed_at   = Column(DateTime, nullable=True)

