import uuid
import enum
from sqlalchemy import Column, String, DateTime, Date, Integer, ForeignKey, Text, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base
from utils.timezone import app_now
from models.location import UnitType, ZoneType, UNIT_TYPE_LABELS, ZONE_LABELS


class BlockItemStatus(str, enum.Enum):
    blocked  = "blocked"
    released = "released"


BLOCK_ITEM_STATUS_LABELS = {
    BlockItemStatus.blocked:  "Blocked",
    BlockItemStatus.released: "Released",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Block Tags — hold specific tags off the sellable market for a pending deal
#  or customer, without actually selling them. A blocked tag stays visible on
#  Ready to Sale (so stock levels still read correctly) but its Sell action is
#  refused by services/control_engine.validate_sale_allowed() until released.
# ─────────────────────────────────────────────────────────────────────────────

class BlockRecord(Base):
    """One row per "New Block Tags" submission — the shared header (Block ID,
    Date, Container, Location, Block For, Customer) for however many tags were
    selected in that submission. Per-tag status (Blocked/Done/Released) lives
    on BlockRecordItem below, not here, since Bulk Done/Release can act on
    individual tags within the same block independently of one another."""
    __tablename__ = "block_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Format: <DDMMYYYY>-<CONTAINER>-<4-digit incremental>, e.g. 01102026-SHELF-0001.
    # The incremental counter resets per (block_date, container) pair — see
    # _next_block_id() in routers/block_tags.py.
    block_id = Column(String(40), nullable=False, unique=True, index=True)
    block_date = Column(Date, nullable=False, index=True)
    container = Column(SAEnum(UnitType), nullable=False)
    # Optional physical box the blocked tags are packed in — shown as a badge
    # next to the Block ID and printed on the barcode label.
    box_number = Column(String(50), nullable=True)
    location_zone = Column(SAEnum(ZoneType), nullable=True, index=True)
    block_for_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    customer_name = Column(String(150), nullable=True)
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=app_now)

    block_for = relationship("User", foreign_keys=[block_for_user_id])
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    items = relationship("BlockRecordItem", back_populates="block_record", lazy="select")

    @property
    def container_label(self):
        return UNIT_TYPE_LABELS.get(self.container, self.container)

    @property
    def location_zone_label(self):
        return ZONE_LABELS.get(self.location_zone, self.location_zone) if self.location_zone else "—"


class BlockRecordItem(Base):
    """One row per individual tag under a block — this is what the "Tags
    Blocked" table actually lists (each row shows its parent BlockRecord's
    shared fields plus this tag's own snapshot + status). Device fields are
    snapshotted at block time, same pattern as AuditRecordItem."""
    __tablename__ = "block_record_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    block_record_id = Column(UUID(as_uuid=True), ForeignKey("block_records.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    barcode     = Column(String(100), nullable=False)
    lot_number  = Column(String(50), nullable=True)
    model       = Column(String(100), nullable=True)
    cpu         = Column(String(100), nullable=True)
    ram_gb      = Column(Integer, nullable=True)
    storage_gb  = Column(Integer, nullable=True)

    status = Column(SAEnum(BlockItemStatus, name="blockitemstatus"), nullable=False,
                     default=BlockItemStatus.blocked, index=True)
    status_changed_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    status_changed_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=app_now)

    block_record = relationship("BlockRecord", back_populates="items")
    device = relationship("Device", foreign_keys=[device_id])
    status_changed_by = relationship("User", foreign_keys=[status_changed_by_user_id])

    @property
    def status_label(self):
        return BLOCK_ITEM_STATUS_LABELS.get(self.status, self.status)
