"""IQC snapshots (2026-11-22, Pankaj — approved new table).

A tag's live IQC (IQCInspection + the Device's spec fields) is the "TRC IQC":
every repair / Final QC / return change keeps updating it, as before. This
table keeps frozen copies at two moments, never edited by the repair flow:

  NEW  — the IQC as it came in: written when the tag first gets its IQC
         (IQC form, bulk upload, OxyQC) and kept in step while the tag is
         still in intake (GRN / IQC); frozen once it moves on. One per tag.
  SOLD — the IQC at each sale (first or repeat), linked to that Sale row.

`iqc` holds every IQCInspection field plus the tag's spec fields; `context`
the GRN / Lot / Sub-lot / Invoice / Location at that moment. Both are JSON so
IQC fields added later are captured with no change here. Written by
services/iqc_snapshot.py; shown by Device Detail → IQC Compare.
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base
from utils.timezone import app_now


class IQCSnapshot(Base):
    __tablename__ = "iqc_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    kind = Column(String(10), nullable=False)                 # NEW / SOLD
    sale_id = Column(UUID(as_uuid=True), ForeignKey("sales.id"), nullable=True)  # SOLD only
    taken_at = Column(DateTime, nullable=False, default=app_now)
    taken_by = Column(String(100), nullable=True)
    source = Column(String(40), nullable=True)                # IQC / Sale / ...
    iqc = Column(JSON, nullable=False)
    context = Column(JSON, nullable=True)
    backfilled = Column(Boolean, nullable=False, default=False)

    # Relationships only so a copy saved in the same flush as its new Device /
    # Sale is inserted after it (FK order); no schema effect.
    device = relationship("Device", foreign_keys=[device_id])
    sale = relationship("Sale", foreign_keys=[sale_id])

    __table_args__ = (
        Index("ix_iqc_snapshots_device_kind", "device_id", "kind"),
        Index("ix_iqc_snapshots_sale_id", "sale_id"),
    )
