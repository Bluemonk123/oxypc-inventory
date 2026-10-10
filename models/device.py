import uuid
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Integer, Float, Boolean, ForeignKey, Enum as SAEnum, Text, Numeric, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum
from database import Base


class DeviceStage(str, enum.Enum):
    grn      = "grn"
    iqc      = "iqc"
    stock_in = "stock_in"
    l1 = "l1"
    l2 = "l2"
    l3 = "l3"
    trc_production = "trc_production"
    qc_check = "qc_check"
    cosmetic_received = "cosmetic_received"
    cleaning = "cleaning"
    putty = "putty"
    dry_sanding = "dry_sanding"
    masking = "masking"
    painting = "painting"
    water_sanding = "water_sanding"
    cosmetic_completed = "cosmetic_completed"
    final_qc = "final_qc"
    final_qc_pass_hold = "final_qc_pass_hold"
    final_qc_fail_hold = "final_qc_fail_hold"
    ready_to_sale = "ready_to_sale"
    sold = "sold"
    returned = "returned"
    scrapped = "scrapped"
    scrap_for_sale = "scrap_for_sale"


class DeviceGrade(str, enum.Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    scrap = "scrap"


STAGE_LABELS = {
    DeviceStage.grn: "GRN Receipt",
    DeviceStage.iqc: "IQC",
    DeviceStage.stock_in: "Stock In",
    DeviceStage.l1: "L1/L2 Repair",
    DeviceStage.l2: "L2 Repair",
    DeviceStage.l3: "L3/L4 Repair",
    DeviceStage.trc_production: "TRC Production",
    DeviceStage.qc_check: "Stress Test",
    DeviceStage.cosmetic_received: "Cosmetic Received",
    DeviceStage.cleaning: "Cleaning",
    DeviceStage.putty: "Putty",
    DeviceStage.dry_sanding: "Dry Sanding",
    DeviceStage.masking: "Masking",
    DeviceStage.painting: "Painting",
    DeviceStage.water_sanding: "Water Sanding",
    DeviceStage.cosmetic_completed: "Cosmetic Completed",
    DeviceStage.final_qc: "Final QC",
    DeviceStage.final_qc_pass_hold: "Final QC — Pass Hold",
    DeviceStage.final_qc_fail_hold: "Final QC — Fail Hold",
    DeviceStage.ready_to_sale: "Ready to Sale",
    DeviceStage.sold: "Sold",
    DeviceStage.returned: "Returned",
    DeviceStage.scrapped: "Scrapped",
    DeviceStage.scrap_for_sale: "Scrap for Sale",
}

# Every Stage dropdown/filter across the app builds its option list from this,
# not the raw DeviceStage enum — l2 is a legacy stage (the merged L1/L2 page
# replaced the old l1->l2->l3 ladder; see routers/repair.py's STAGE_MAP/
# NEXT_STAGE comments) that no device is ever newly moved into, so it stays
# out of every stage-picking UI while remaining a valid enum member for any
# device still sitting in it historically. `sold` is excluded too (2026-09-21):
# this list feeds every generic "move device to any stage" tool (Manual Stage
# Movement, the IQC/Bulk Customise modal's bulk Move-to-Stage) — none of them
# create the Sale row that "sold" actually requires, so picking it there
# orphans the device at current_stage=sold with zero matching Sale records,
# which then makes Process Return fail with "No sale found for this device"
# for a tag that was never really sold. Selling only ever happens through the
# real Sale-creation flow (routers/sales.py mark_sold), which sets this stage
# itself alongside the Sale row — it doesn't need this dropdown at all.
DROPDOWN_STAGES = [s for s in DeviceStage if s not in (DeviceStage.l2, DeviceStage.sold)]

# The paint-line stages plus Putty, grouped as "Cosmetic" wherever the app
# needs one count/filter for that whole phase (Dashboard's Stage Pipeline,
# Production Manager's summary tiles) — shared here so the two never drift
# into counting a different set of stages under the same "Cosmetic" label.
COSMETIC_STAGES = [
    DeviceStage.cosmetic_received,
    DeviceStage.cleaning, DeviceStage.putty, DeviceStage.dry_sanding,
    DeviceStage.masking, DeviceStage.painting, DeviceStage.water_sanding,
    DeviceStage.cosmetic_completed,
]

STAGE_COLORS = {
    DeviceStage.grn: "warning",
    DeviceStage.iqc: "secondary",
    DeviceStage.stock_in: "info",
    DeviceStage.l1: "warning",
    DeviceStage.l2: "warning",
    DeviceStage.l3: "danger",
    DeviceStage.trc_production: "info",
    DeviceStage.qc_check: "primary",
    DeviceStage.cosmetic_received: "teal",
    DeviceStage.cleaning: "teal",
    DeviceStage.putty: "teal",
    DeviceStage.dry_sanding: "teal",
    DeviceStage.masking: "teal",
    DeviceStage.painting: "teal",
    DeviceStage.water_sanding: "teal",
    DeviceStage.cosmetic_completed: "teal",
    DeviceStage.final_qc: "purple",
    DeviceStage.final_qc_pass_hold: "success",
    DeviceStage.final_qc_fail_hold: "warning",
    DeviceStage.ready_to_sale: "success",
    DeviceStage.sold: "dark",
    DeviceStage.returned: "warning",
    DeviceStage.scrapped: "danger",
    DeviceStage.scrap_for_sale: "dark",
}


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    barcode = Column(String(100), unique=True, nullable=False, index=True)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=False, index=True)
    brand = Column(String(50), nullable=True, index=True)
    entity = Column(String(30), nullable=True, index=True)  # OxyPC Computers / Renew Circuits — additive, auto-provisioned by db_validator
    model = Column(String(100), nullable=True)
    device_type = Column(String(30), nullable=True, index=True)  # exact-match filter + sort column on /devices — see Global Table query optimization
    invoice_number = Column(String(100), nullable=True)  # bulk-set via Product IQC Customise modal
    po_number = Column(String(100), nullable=True)        # bulk-set via Product IQC Customise modal
    serial_no = Column(String(100), nullable=True)
    sub_category = Column(String(20), nullable=True, index=True)   # Laptop / Desktop / TFT
    cpu = Column(String(100), nullable=True)
    cpu_make = Column(String(100), nullable=True)   # Intel / AMD / Apple (additive, auto-provisioned by db_validator)
    generation = Column(String(50), nullable=True)
    ram_gb = Column(Integer, nullable=True)
    ram_type = Column(String(20), nullable=True)
    ram_speed = Column(String(20), nullable=True)
    ram_make = Column(String(50), nullable=True)
    storage_gb = Column(Integer, nullable=True)
    storage_type = Column(String(20), nullable=True)
    hdd_capacity_gb = Column(Integer, nullable=True)   # separate HDD (if dual storage)
    hdd_type = Column(String(20), nullable=True)
    hdd_speed = Column(String(20), nullable=True)
    ram_summary = Column(String(255), nullable=True)   # packed multi-stick string, e.g. "16GB_DDR4_2300MHz_Samsung, 8GB_DDR4_2133MHz_Crucial"
    hdd_summary = Column(String(255), nullable=True)    # packed multi-drive string, e.g. "520GB_SSD_5400RPM_Samsung, 1TB_SSD_7200RPM_Seagate"
    # ── IQC RAM/HDD count + size (additive, auto-provisioned by db_validator) ──
    total_ram_count = Column(String(50), nullable=True)
    total_ram_size  = Column(String(50), nullable=True)
    total_hdd_count = Column(String(50), nullable=True)
    total_hdd_size  = Column(String(50), nullable=True)
    battery_health_pct = Column(Integer, nullable=True)  # 0-100 for laptops
    screen_size = Column(String(20), nullable=True)    # e.g. "14.0 FHD"
    color = Column(String(30), nullable=True)
    bios_password = Column(Boolean, default=False, nullable=True)
    grade = Column(SAEnum(DeviceGrade), nullable=True)
    final_qc_status = Column(String(10), nullable=True, index=True)  # "pass" / "fail" — set on Final QC decision
    fqc_failure_reason = Column(Text, nullable=True)  # set when final_qc_status="fail"; shown on Devices Failed / Final QC Fail (Bucket) tables
    fqc_pass_notes = Column(Text, nullable=True)      # set when final_qc_status="pass"; shown on Devices Passed table
    fqc_final_notes = Column(Text, nullable=True)     # raw "Final Notes" field from the Final QC fail form; shown as its own column on Devices Failed
    touch_type = Column(String(10), nullable=True)    # "touch" / "non_touch" — set by the Final QC form; blank shows as NT (Non-Touch)
    current_stage = Column(SAEnum(DeviceStage), nullable=False, default=DeviceStage.iqc, index=True)
    floor = Column(String(50), nullable=True)          # holds the Zone value (ZoneType) selected on IQC/Edit
    warehouse = Column(String(100), nullable=True)     # legacy free-text; now best-effort mirrors location display_name
    location_id = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True)  # precise StorageLocation FK
    grn_number = Column(String(50), nullable=True, index=True)     # Goods Receipt Note ref
    return_status = Column(Boolean, nullable=False, default=False, server_default=text("false"))  # True once returned via Process Return
    replaced = Column(String(120), nullable=True)      # "Replaced by <tag>" / "Replaced from <tag>" (L3 device swap)
    replace_with_barcode = Column(String(100), nullable=True)  # Scrap Products "Replace" modal — selected replacement tag (Replacement Scrap flow)
    # ── Return New page / Change Floor / GRN Post-IQC "As-Is Lot" (2026-09-22) ──
    # Free text, exactly one of: "Return for Repair" / "Replaced by <tag>" —
    # set by the Internal Tag tab (process_return) — or "Return for Credit
    # Note" — set by the Credit Note tab (process_credit_note), the only
    # place that value comes from (2026-09-24). Drives the Inventory
    # Manager / Production Manager "Credit Note" table and the Production
    # Manager "Tags Returned" tile.
    tag_return_status = Column(String(120), nullable=True)
    # Set by Change Floor's "As-Is Lot" transfer (New Sub-Lot radio) or GRN
    # Post-IQC's "Yes As-Is" modal — both bulk-write this across every Tag
    # Number they apply to, distinct from the device's actual Lot Number.
    sub_lot_number = Column(String(50), nullable=True)
    # Ready to Sale "As-Is Lot" table's Edit modal (2026-09-22) — bulk-set
    # together across every Tag Number sharing the same (lot_id, sub_lot_number)
    # pair, same write pattern as sub_lot_number above. Blank until first set;
    # no default/computed value.
    min_selling_price = Column(Numeric(12, 2), nullable=True)
    max_selling_price = Column(Numeric(12, 2), nullable=True)
    # Set in bulk by GRN Post-IQC's "Yes As-Is" modal or by /transfers when
    # Transfer Type is Ready for Sale / As-Is Lot / Scrap for Sale — the
    # employee this Tag Number is currently assigned to. Additive; auto-
    # provisioned by db_validator.
    assigned_to_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    # ── L1/L2 → L3/L4 status-driven hand-off flow (additive; auto-provisioned by db_validator) ──
    l1l2_status = Column(String(30), nullable=True, default="New")   # New | Repair Started | Requested to L3/L4
    l34_status  = Column(String(30), nullable=True)                  # (blank) | Repair Started | Completed | Normal Scrap | Replacement Scrap
    l34_activity = Column(String(100), nullable=True)                # Activities dropdown, set on L3/L4 Mark Complete
    device_price = Column(Numeric(12, 2), nullable=True)  # Individual device buying price
    qty           = Column(Integer, nullable=True, server_default="1")  # Units this record covers (default 1)
    lot_line_item_id = Column(UUID(as_uuid=True), ForeignKey("lot_line_items.id"), nullable=True)
    notes = Column(Text, nullable=True)
    # Repair-authored note captured on the Request to L3/L4 modal; surfaced as the Repair Notes
    # column on L1/L2, L3/L4, Final QC and Device Detail. Distinct from `notes` above, which is
    # the generic device note. Additive; auto-provisioned by db_validator.
    repair_notes = Column(Text, nullable=True)
    # Stress Test failure note — built from the failed checklist items + any manual note typed
    # on the Fail modal, set by POST /stress/{barcode}/fail. Cleared (None) on Complete, mirroring
    # fqc_failure_reason/fqc_pass_notes' single-current-value convention. Surfaced as the Stress
    # Notes column on L1/L2 Repair (after Repair Notes) and on Device Detail. Additive;
    # auto-provisioned by db_validator.
    stress_notes = Column(Text, nullable=True)
    scrap_verified = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_active  = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    deleted_at = Column(DateTime, nullable=True)
    is_trashed = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    trashed_at = Column(DateTime, nullable=True)
    # True while this device has an open (not Done/Released) Block Tags hold —
    # see models/block_tags.py. Stays visible on Ready to Sale (stock levels
    # still read correctly) but services/control_engine.validate_sale_allowed()
    # refuses the actual Sell action while this is True.
    is_blocked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    bucket_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    partner_listed = Column(Boolean, nullable=False, default=False, server_default=text("false"))  # backing a live Trade Partner listing
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now, index=True)

    lot = relationship("Lot", back_populates="devices")
    stage_movements = relationship("StageMovement", back_populates="device", lazy="select")
    repair_jobs = relationship("RepairJob", back_populates="device", lazy="select")
    qc_checks = relationship("QCCheck", back_populates="device", lazy="select")
    sales = relationship("Sale", back_populates="device", lazy="select")
    spare_parts_consumption = relationship("SparePartConsumption", back_populates="device", lazy="select")
    ram_tracking = relationship("RAMTracking", back_populates="device", foreign_keys="RAMTracking.device_id", lazy="select")

    @property
    def stage_label(self):
        return STAGE_LABELS.get(self.current_stage, self.current_stage)

    @property
    def stage_color(self):
        return STAGE_COLORS.get(self.current_stage, "secondary")


class StageMovement(Base):
    __tablename__ = "stage_movements"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id  = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)
    from_stage = Column(SAEnum(DeviceStage), nullable=True)
    to_stage   = Column(SAEnum(DeviceStage), nullable=False, index=True)
    moved_by   = Column(String(50), nullable=True)
    moved_at   = Column(DateTime, default=app_now)
    exited_at  = Column(DateTime, nullable=True)   # set when device leaves this stage
    notes      = Column(Text, nullable=True)

    device = relationship("Device", back_populates="stage_movements")


class MovementDirection(str, enum.Enum):
    send = "send"
    sold = "sold"


class TagNumberMovement(Base):
    """One row per bulk Change-Entity action on the Entity Movement page —
    NOT one row per tag number. `tag_count` is the number of tags moved in
    that single action."""
    __tablename__ = "tag_number_movements"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    moved_at         = Column(DateTime, default=app_now)
    tag_count        = Column(Integer, nullable=False)
    from_entity      = Column(String(30), nullable=True)
    to_entity        = Column(String(30), nullable=False)
    direction        = Column(SAEnum(MovementDirection), nullable=False)
    by_user_id       = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    by_user_name     = Column(String(100), nullable=True)  # denormalized, survives user deletion
    invoice_pdf_path = Column(String(255), nullable=True)
    notes            = Column(String(255), nullable=True)
