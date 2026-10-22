import uuid
import enum
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Text, Integer, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class ZoneType(str, enum.Enum):
    showroom        = "showroom"
    ground_floor    = "ground_floor"
    first_floor     = "first_floor"
    second_floor    = "second_floor"
    workshop        = "workshop"
    dispatch        = "dispatch"
    warehouse       = "warehouse"
    holding         = "holding"


ZONE_LABELS = {
    ZoneType.showroom:     "Showroom",
    ZoneType.ground_floor: "Ground Floor",
    ZoneType.first_floor:  "1st Floor",
    ZoneType.second_floor: "2nd Floor",
    ZoneType.workshop:     "TRC Floor",
    ZoneType.dispatch:     "Dispatch Area",
    ZoneType.warehouse:    "Plant-Floor",   # was "Warehouse"; renamed 2026-10-22 (only PLANT-FLOOR uses it). Stored value unchanged.
    ZoneType.holding:      "TRC Store",
}


class UnitType(str, enum.Enum):
    rack    = "rack"
    crate   = "crate"
    shelf   = "shelf"
    trolley = "trolley"
    cabinet = "cabinet"
    floor   = "floor"


UNIT_TYPE_LABELS = {
    UnitType.rack:    "Rack",
    UnitType.crate:   "Crate",
    UnitType.shelf:   "Shelf",
    UnitType.trolley: "Trolley",
    UnitType.cabinet: "Cabinet",
    UnitType.floor:   "Floor Space",
}


class LocationAction(str, enum.Enum):
    assigned     = "assigned"    # initial placement (no prior location)
    picked_up    = "picked_up"   # device removed from location by a user
    placed_back  = "placed_back" # device returned to a location
    moved        = "moved"       # relocated to a different location


class AuditStatus(str, enum.Enum):
    pending     = "pending"
    in_progress = "in_progress"
    completed   = "completed"


class ScanStatus(str, enum.Enum):
    found       = "found"       # expected and physically seen
    missing     = "missing"     # expected but not found during audit
    extra       = "extra"       # found physically but not expected at this location


# ─────────────────────────────────────────────────────────────────────────────
#  StorageLocation — master registry of every physical bin/crate/rack/slot
# ─────────────────────────────────────────────────────────────────────────────
class StorageLocation(Base):
    __tablename__ = "storage_locations"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zone        = Column(SAEnum(ZoneType), nullable=False)
    unit_type   = Column(SAEnum(UnitType), nullable=False)
    unit_id     = Column(String(50), nullable=False, unique=True)   # e.g. RACK-A1, CRATE-G3
    slot        = Column(String(20), nullable=True)                 # optional sub-slot within unit
    description = Column(String(200), nullable=True)
    capacity    = Column(Integer, nullable=True)                    # max devices (optional)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=app_now)

    location_logs = relationship("DeviceLocationLog", back_populates="location", lazy="select")
    audit_scans   = relationship("AuditScanItem", back_populates="location", lazy="select")

    @property
    def zone_label(self):
        return ZONE_LABELS.get(self.zone, self.zone)

    @property
    def unit_type_label(self):
        return UNIT_TYPE_LABELS.get(self.unit_type, self.unit_type)

    @property
    def display_name(self):
        base = f"{self.zone_label} → {self.unit_type_label} {self.unit_id}"
        return f"{base} [{self.slot}]" if self.slot else base


# ─────────────────────────────────────────────────────────────────────────────
#  DeviceLocationLog — every pick-up / put-back / move event
# ─────────────────────────────────────────────────────────────────────────────
class DeviceLocationLog(Base):
    __tablename__ = "device_location_logs"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id   = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    location_id = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True)
    action      = Column(SAEnum(LocationAction), nullable=False)
    actor_id    = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    actor_name  = Column(String(100), nullable=True)   # denormalised for fast display
    notes       = Column(Text, nullable=True)
    logged_at   = Column(DateTime, default=app_now, index=True)

    device   = relationship("Device", foreign_keys=[device_id])
    location = relationship("StorageLocation", back_populates="location_logs")
    actor    = relationship("User", foreign_keys=[actor_id])

    @property
    def action_label(self):
        labels = {
            LocationAction.assigned:    "Assigned",
            LocationAction.picked_up:   "Picked Up",
            LocationAction.placed_back: "Placed Back",
            LocationAction.moved:       "Moved",
        }
        return labels.get(self.action, self.action)

    @property
    def action_color(self):
        colors = {
            LocationAction.assigned:    "info",
            LocationAction.picked_up:   "warning",
            LocationAction.placed_back: "success",
            LocationAction.moved:       "primary",
        }
        return colors.get(self.action, "secondary")


# ─────────────────────────────────────────────────────────────────────────────
#  InventoryAudit — one physical audit session (monthly or ad-hoc)
# ─────────────────────────────────────────────────────────────────────────────
class InventoryAudit(Base):
    __tablename__ = "inventory_audits"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_number   = Column(String(30), unique=True, nullable=False)   # e.g. AUD-2026-03
    zone_filter    = Column(String(50), nullable=True)                 # None = all zones
    status         = Column(SAEnum(AuditStatus), default=AuditStatus.pending)
    initiated_by   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    initiated_by_name = Column(String(100), nullable=True)
    initiated_at   = Column(DateTime, default=app_now)
    completed_at   = Column(DateTime, nullable=True)
    notes          = Column(Text, nullable=True)

    # Summary counters (populated on completion)
    expected_count = Column(Integer, default=0)
    found_count    = Column(Integer, default=0)
    missing_count  = Column(Integer, default=0)
    extra_count    = Column(Integer, default=0)

    initiator  = relationship("User", foreign_keys=[initiated_by])
    scan_items = relationship("AuditScanItem", back_populates="audit", lazy="select")


# ─────────────────────────────────────────────────────────────────────────────
#  AuditScanItem — individual device result within an audit
# ─────────────────────────────────────────────────────────────────────────────
class AuditScanItem(Base):
    __tablename__ = "audit_scan_items"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id        = Column(UUID(as_uuid=True), ForeignKey("inventory_audits.id"), nullable=False)
    device_id       = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True)
    barcode_scanned = Column(String(100), nullable=False)
    location_id     = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True)
    scan_status     = Column(SAEnum(ScanStatus), nullable=False)
    scanned_by      = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    scanned_by_name = Column(String(100), nullable=True)
    scanned_at      = Column(DateTime, default=app_now)
    notes           = Column(Text, nullable=True)

    audit    = relationship("InventoryAudit", back_populates="scan_items")
    device   = relationship("Device", foreign_keys=[device_id])
    location = relationship("StorageLocation", back_populates="audit_scans")
    scanner  = relationship("User", foreign_keys=[scanned_by])
