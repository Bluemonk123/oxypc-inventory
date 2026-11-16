"""Part Estimation — a costed, per-lot bill of the spare parts the lot's IQC
data says are required.

One PartEstimate per generated estimate (a lot can be re-estimated; each run
creates a new immutable row and its own Excel file), with one
PartEstimateLine per part name. Generating an estimate also raises a
PartSourcingRequest carrying source='production' so the Spare Parts Manager
sees it on the Part Master -> Sourcing Requests tab.
"""
import uuid

from sqlalchemy import Column, String, DateTime, Integer, Numeric, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base
from utils.timezone import app_now


class PartEstimate(Base):
    __tablename__ = "part_estimates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    lot_id = Column(UUID(as_uuid=True), ForeignKey("lots.id"), nullable=False, index=True)
    lot_number = Column(String(100), nullable=True)          # snapshot for display

    total_products = Column(Integer, nullable=False, default=0)   # tags in the lot at generation time
    total_qty = Column(Integer, nullable=False, default=0)        # sum of part quantities
    parts_total_cost = Column(Numeric(14, 2), nullable=False, default=0)
    labour_cost = Column(Numeric(14, 2), nullable=False, default=0)
    total_estimation = Column(Numeric(14, 2), nullable=False, default=0)

    # DeviceGrade value ("A".."E", "scrap") this file was generated for. The
    # Select Grade dropdown on Part Estimate accepts more than one grade in a
    # single click — one identical-content workbook is written per grade
    # chosen, so this is what tells two otherwise-identical files apart, and
    # what the Download column shows in place of the generic word "Estimate".
    grade = Column(String(10), nullable=True)
    notes = Column(Text, nullable=True)
    file_path = Column(String(255), nullable=True)           # uploads/part_estimates/<uuid>.xlsx
    file_name = Column(String(255), nullable=True)           # human-facing download name

    # 'flat' = Generate Estimate, 'model_matrix' = Part Estimate,
    # 'checklist' = Create Estimate, 'open' = Open Estimate. Lets the lot
    # row's Download list and any future filtering tell the output types
    # apart without re-deriving it from which lines table got rows.
    estimate_type = Column(String(20), nullable=False, default="flat")

    # Open Estimate extras (2026-11-15, Pankaj — approved):
    #   row_specs    JSON {part: chosen size} — the RAM / Storage / Screen Size
    #                dropdown next to those part names (Master Data options)
    #   store_inputs JSON {part: count} — entered on Part Master -> Sourcing
    #                Requests -> Confirm Request (Store Inputs column)
    #   status       "" until that confirm, then "Updated" (shown on both pages;
    #                the attached sheet is rewritten with Difference / Store Inputs)
    row_specs = Column(Text, nullable=True)
    store_inputs = Column(Text, nullable=True)
    status = Column(String(20), nullable=True)

    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=app_now)

    lines = relationship("PartEstimateLine", back_populates="estimate",
                         lazy="select", cascade="all, delete-orphan")
    checklist_lines = relationship("PartEstimateChecklistLine", back_populates="estimate",
                                   lazy="select", cascade="all, delete-orphan")
    open_lines = relationship("PartEstimateOpenLine", back_populates="estimate",
                              lazy="select", cascade="all, delete-orphan")


class PartEstimateLine(Base):
    __tablename__ = "part_estimate_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    estimate_id = Column(UUID(as_uuid=True), ForeignKey("part_estimates.id"),
                         nullable=False, index=True)

    part_name = Column(String(150), nullable=False)
    qty = Column(Integer, nullable=False, default=0)
    unit_cost = Column(Numeric(12, 2), nullable=False, default=0)
    total_cost = Column(Numeric(14, 2), nullable=False, default=0)

    estimate = relationship("PartEstimate", back_populates="lines", lazy="select")


class PartEstimateChecklistLine(Base):
    """One row per (field, model) cell priced on a Create Estimate — the
    checklist-based matrix (Critical/Hardware/Display Panel/etc., one column
    per Model in the lot). Kept separate from PartEstimateLine because a
    checklist cell carries a field-group/field-name/model identity that a
    flat "<model> — <part>" string would lose, and this table is what lets a
    generated estimate be re-displayed or re-derived exactly later."""
    __tablename__ = "part_estimate_checklist_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    estimate_id = Column(UUID(as_uuid=True), ForeignKey("part_estimates.id"),
                         nullable=False, index=True)

    field_group = Column(String(50), nullable=False)     # e.g. "Display Panel"
    field_name = Column(String(100), nullable=False)     # e.g. "Scratch"
    model_name = Column(String(150), nullable=False)

    # "minor" / "major" / "both" — the severity checkbox state active when
    # this cell's count was computed, for audit/regeneration fidelity.
    severity_scope = Column(String(10), nullable=True)

    count = Column(Integer, nullable=False, default=0)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    line_total = Column(Numeric(14, 2), nullable=False, default=0)

    estimate = relationship("PartEstimate", back_populates="checklist_lines", lazy="select")


class PartEstimateOpenLine(Base):
    """One row per (part, model) cell filled on Open Estimate — the free-form
    parts x models grid (Part Estimation -> "Open Estimate"). Unlike the other
    matrices, cell values are never derived from IQC; the operator types
    directly into the grid, so only cells actually filled are stored. Model
    count and Repair Charge are repeated on every line for that model rather
    than kept in a second table, so a re-open can prefill both the grid and
    the per-model Repair Charge row from the lot's last Open Estimate with one
    query."""
    __tablename__ = "part_estimate_open_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    estimate_id = Column(UUID(as_uuid=True), ForeignKey("part_estimates.id"),
                         nullable=False, index=True)

    part_name = Column(String(150), nullable=False)
    model_key = Column(String(255), nullable=False)
    model_name = Column(String(150), nullable=False)
    model_count = Column(Integer, nullable=False, default=0)

    qty = Column(Numeric(12, 2), nullable=False, default=0)
    repair_charge = Column(Numeric(12, 2), nullable=False, default=0)

    estimate = relationship("PartEstimate", back_populates="open_lines", lazy="select")
