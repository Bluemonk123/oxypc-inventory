import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Date, Integer, ForeignKey, Text, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base
from models.device import DeviceStage, STAGE_LABELS


# ─────────────────────────────────────────────────────────────────────────────
#  Physical Audits — deliberately separate from the older InventoryAudit /
#  AuditScanItem models (models/location.py), which back the zone/scan-batch
#  "Physical Audit" tool under Inventory Locations (relabelled "Zone Audit" in
#  the sidebar so the two features don't read as duplicates). Do not merge.
# ─────────────────────────────────────────────────────────────────────────────


class AuditRecord(Base):
    """One row per unique (Report Date, My Stage, My Location, Category)
    combination — the auditor's claimed stage/location plus the scanned
    device's own category, not the device's actual system stage/location
    (that comparison still happens per-tag, see AuditRecordItem below).
    `current_count` is a snapshot — computed live from the Device table — of
    how many devices actually match that stage+location+category the last
    time this row was touched; `physical_count` increments by 1 every time a
    tag audit (AuditRecordItem) is submitted against it.

    Originally grouped by System Stage/System Location instead (columns of
    the same name still sit unused in the DB — additive-only schema, nothing
    dropped). Changed because grouping by what the auditor actually claimed
    is what a physical count reconciliation needs; System Stage/System
    Location are still captured and compared per-tag on AuditRecordItem."""
    __tablename__ = "audit_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_date = Column(Date, nullable=False, index=True)
    # Nullable at the DB level (unlike AuditRecordItem.my_stage) only so this
    # new column can be ADD COLUMN'd onto a table that may already have rows
    # from before this field existed, without a backfill — always required
    # at the application layer (routers/physical_audits.py validates it via
    # Form(...) before ever constructing a row).
    my_stage = Column(SAEnum(DeviceStage, name="devicestage"), nullable=True, index=True)
    my_location_id = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True, index=True)
    category = Column(String(100), nullable=True, index=True)
    # L1/L2 and L3/L4 audit rows (2026-10-24, schema change approved: "I approve
    # dbdiagram"): "PNA" = tags with an active PNA part, "Line" = the rest.
    # NULL for every other stage (and rows saved before).
    pna_group = Column(String(10), nullable=True)
    current_count = Column(Integer, nullable=False, default=0)
    physical_count = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    my_location = relationship("StorageLocation", foreign_keys=[my_location_id])
    items = relationship("AuditRecordItem", back_populates="audit_record", lazy="select")

    @property
    def difference(self):
        """current_count - physical_count, computed on read rather than
        stored — always accurate, never denormalised/stale."""
        return (self.current_count or 0) - (self.physical_count or 0)

    @property
    def my_stage_label(self):
        return STAGE_LABELS.get(self.my_stage, self.my_stage)


class AuditRecordItem(Base):
    """One row per individual Tag Number audited (the "Done" submission),
    child of AuditRecord. Device fields are denormalised snapshots taken at
    audit time — Device data can change later and the audit should reflect
    what was true when it was performed."""
    __tablename__ = "audit_record_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_record_id = Column(UUID(as_uuid=True), ForeignKey("audit_records.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    # NULL = the scanned Tag Number doesn't exist in the system: recorded as
    # Missing (2026-10-23, schema change approved: "i approve dbdiagram").
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)

    # ── Snapshot at audit time ────────────────────────────────────────────
    barcode = Column(String(100), nullable=False)
    lot_number = Column(String(50), nullable=True)
    brand = Column(String(50), nullable=True)
    model = Column(String(100), nullable=True)
    serial_no = Column(String(100), nullable=True)
    category = Column(String(100), nullable=True)

    system_stage = Column(SAEnum(DeviceStage, name="devicestage"), nullable=True)   # NULL for a Missing (unknown) tag
    system_location_id = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True)
    my_stage = Column(SAEnum(DeviceStage, name="devicestage"), nullable=False)
    my_location_id = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True)

    # Who this tag was assigned to/by at audit time. `assigned_by_user_id` is
    # currently always NULL — see routers/physical_audits.py's module
    # docstring for why no reliable "who performed the assignment" source
    # exists on Device today. Column kept per spec for forward compatibility.
    assigned_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    assigned_to_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    notes = Column(Text, nullable=True)
    audited_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=app_now)

    audit_record = relationship("AuditRecord", back_populates="items")
    device = relationship("Device", foreign_keys=[device_id])
    system_location = relationship("StorageLocation", foreign_keys=[system_location_id])
    my_location = relationship("StorageLocation", foreign_keys=[my_location_id])
    assigned_by = relationship("User", foreign_keys=[assigned_by_user_id])
    assigned_to = relationship("User", foreign_keys=[assigned_to_user_id])
    auditor = relationship("User", foreign_keys=[audited_by_user_id])

    @property
    def system_stage_label(self):
        return STAGE_LABELS.get(self.system_stage, self.system_stage)

    @property
    def my_stage_label(self):
        return STAGE_LABELS.get(self.my_stage, self.my_stage)
