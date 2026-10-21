import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Date, Integer, Numeric, Boolean, ForeignKey, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import UUID
from database import Base


class GRNImport(Base):
    """A GRN created by uploading an invoice PDF. Fields are best-effort extracted
    from the PDF and editable. GRN number is a 12-digit auto-generated id."""
    __tablename__ = "grn_imports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grn_number = Column(String(12), unique=True, nullable=False, index=True)  # 12-digit

    lot_number = Column(String(60), nullable=True)       # parsed from invoice PDF (best-effort, free text)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=True, index=True)  # first mapped Lot — kept for backward compatibility; grn_lots (GRNLot) is the source of truth since 2026-10-21
    # Entity this GRN's goods belong to (Master Data entity — OxyPC Computers /
    # Renew Circuits / Deshwal). Chosen in the GRN modal; copied onto devices by
    # the IQC bulk upload and by ReGRN on the Change Entity page. Approved via
    # dbdiagram 2026-10-21; String(50) to match users.entity / companies.
    entity = Column(String(50), nullable=True)
    # Default storage location for tags received on this GRN (2026-10-21,
    # approved on dbdiagram). IQC bulk upload copies it onto each tag along
    # with the zone / warehouse derived from Location master.
    location_id = Column(UUID(as_uuid=True), ForeignKey("storage_locations.id"), nullable=True)
    invoice_number = Column(String(100), nullable=True)
    invoice_date = Column(String(40), nullable=True)     # text (parsed from PDF, may be free-form)
    sender_name = Column(String(200), nullable=True)     # "Vendor Name" in UI
    quantity = Column(Integer, nullable=True)             # "Invoice Quantity" in UI
    amount = Column(Numeric(14, 2), nullable=True)        # "Invoice Price" in UI
    purchase_date = Column(Date, nullable=True)
    grn_date = Column(Date, nullable=True)
    po_number = Column(String(50), nullable=True)
    vehicle_number = Column(String(50), nullable=True)
    e_way_bill = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    validated = Column(Boolean, default=False, nullable=True)   # set via Validate GRN modal
    received_qty = Column(Integer, nullable=True)               # actual count entered at validation
    validation_ref = Column(String(100), nullable=True)         # GRN reference no. from the modal
    validation_notes = Column(String(500), nullable=True)       # discrepancy / condition remarks
    source = Column(String(20), nullable=True, default="invoice")   # 'invoice' (GRN with Invoice) | 'post_iqc' (GRN post IQC)

    file_name = Column(String(255), nullable=True)
    file_path = Column(String(500), nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)  # SHA-256 for dedupe

    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)

    # Soft delete (compliance: GRN docs are kept + audit-logged, just hidden)
    is_deleted = Column(Boolean, nullable=False, default=False, server_default="false")
    deleted_at = Column(DateTime, nullable=True)


class GRNLot(Base):
    """GRN <-> Lot link, many-to-many (approved via dbdiagram 2026-10-21).

    One lot can be split across several invoices (GRNs), and one GRN can cover
    several lots. Rows are never deleted when a GRN is soft-deleted — like the
    lots themselves, the link stays as history; readers filter out deleted
    GRNs (GRNImport.is_deleted)."""
    __tablename__ = "grn_lots"
    __table_args__ = (UniqueConstraint("grn_id", "lot_id", name="uq_grn_lots_grn_lot"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grn_id = Column(UUID(as_uuid=True), ForeignKey("grn_imports.id"), nullable=False, index=True)
    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=False, index=True)
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, nullable=False, default=app_now)


async def backfill_grn_lots(db) -> int:
    """One-time, idempotent: give every GRN that has the legacy single
    grn_imports.lot_id a matching grn_lots row. Safe to run on every startup —
    pairs already linked are skipped. Returns rows added."""
    have = set((await db.execute(select(GRNLot.grn_id, GRNLot.lot_id))).all())
    pairs = (await db.execute(
        select(GRNImport.id, GRNImport.lot_id).where(GRNImport.lot_id.isnot(None))
    )).all()
    added = 0
    for grn_id, lot_id in pairs:
        if (grn_id, lot_id) in have:
            continue
        db.add(GRNLot(grn_id=grn_id, lot_id=lot_id, created_by="backfill"))
        added += 1
    if added:
        await db.commit()
    return added


GRN_ENTITY_BACKFILL_MARKER = "grn_entity_backfill_2026_10_21"
GRN_ENTITY_BACKFILL_VALUE = "OxyPC Computers"


async def backfill_grn_entity_once(db) -> int:
    """One-time (2026-10-21, Pankaj's decision): every GRN that existed before
    grn_imports.entity was added gets Entity "OxyPC Computers". Guarded by an
    app_settings marker row, so GRNs created later without an Entity are never
    touched. Returns the number of GRNs updated (0 if already done)."""
    from sqlalchemy import update
    from models.settings import AppSetting
    if (await db.execute(
        select(AppSetting).where(AppSetting.key == GRN_ENTITY_BACKFILL_MARKER)
    )).scalar_one_or_none():
        return 0
    res = await db.execute(
        update(GRNImport).where(GRNImport.entity.is_(None))
        .values(entity=GRN_ENTITY_BACKFILL_VALUE)
    )
    db.add(AppSetting(key=GRN_ENTITY_BACKFILL_MARKER, value="1", updated_by="system",
                      description="One-time: existing GRNs set to Entity OxyPC Computers"))
    await db.commit()
    return res.rowcount or 0
